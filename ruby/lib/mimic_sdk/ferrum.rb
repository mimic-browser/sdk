require 'mimic_sdk'

begin
  require 'ferrum'
rescue LoadError => error
  raise LoadError, "Install the selected adapter dependency with `gem install ferrum`: #{error.message}"
end

module MimicSDK
  module Ferrum
    # Identity and typed capabilities for the context being configured.
    # @!attribute [rw] browser_context_id
    #   @return [String]
    # @!attribute [rw] mimic
    #   @return [MimicSDK::Client]
    ContextSetup = Struct.new(:browser_context_id, :mimic, keyword_init: true)

    class Session
      # @return [::Ferrum::Browser]
      attr_reader :browser
      # @return [MimicSDK::Client]
      attr_reader :mimic
      # @return [MimicSDK::RuntimeProcess, nil] nil when attached to an external runtime
      attr_reader :runtime

      # @param endpoint [String] HTTP or browser WebSocket endpoint
      # @param runtime [MimicSDK::RuntimeProcess, nil]
      # @param timeout [Numeric] native client timeout in seconds
      def initialize(endpoint, runtime: nil, timeout: 30)
        @runtime = runtime
        @contexts = []
        @page_clients = {}
        @mutex = Mutex.new
        @close_changed = ConditionVariable.new
        @transport = runtime ? runtime.transport : Transport.new(endpoint, timeout: timeout)
        @mimic = Client.new(@transport)
        version = @mimic.get_version
        raise RuntimeError, 'Endpoint is not Mimic' if version.version.to_s.empty? || version.chrome_version.to_s.empty?
        # Ferrum resolves target creation before its flattened session may be
        # attached. A dedicated target socket binds each Page immediately and
        # avoids caching a SessionClient with a missing session ID.
        @browser = ::Ferrum::Browser.new(
          ws_url: @transport.url, timeout: timeout, process_timeout: timeout, flatten: false
        )
        events, @unsubscribe = @transport.subscribe
        @attachment_events = Thread.new do
          while (event = events.pop)
            next unless event['method'] == 'Target.detachedFromTarget'
            @mutex.synchronize do
              @page_clients.delete_if do |_target, client|
                detached = client.session_id == event.dig('params', 'sessionId')
                detached && client.invalidate
              end
            end
          end
        ensure
          @mutex.synchronize do
            # A pending explicit detach remains owned until its reply/failure.
            @page_clients.delete_if { |_target, client| client.invalidate }
          end
        end
      rescue StandardError
        close
        raise
      end

      # Returns Ferrum::Context, not an SDK automation wrapper. All configuration
      # is complete before a user Page can be created through the returned object.
      # @param profile [String, MimicSDK::Generated::GenerateProfileSelection, Hash]
      # @param media [MimicSDK::Generated::MediaConfiguration, Hash, Proc, nil]
      #   A Proc receives ContextSetup and returns MediaConfiguration or its wire Hash.
      # @param resource_policy [MimicSDK::Generated::ResourcePolicy, Hash, nil]
      # @param proxy [MimicSDK::Generated::Proxy, Hash, nil]
      # @return [::Ferrum::Context]
      def new_context(profile: UNSET, media: nil, resource_policy: nil, proxy: nil)
        context = @mutex.synchronize do
          raise RuntimeError, 'Integration session closed' if @closed
          created = browser.contexts.create
          @contexts << created
          created
        end
        begin
          factory = media.respond_to?(:call)
          # User callbacks run without the ownership mutex and may await their
          # own work, create another Context or close this Session.
          media = media.call(ContextSetup.new(browser_context_id: context.id, mimic: mimic)) if factory
          if factory && !media.is_a?(Hash) && !media.is_a?(Generated::MediaConfiguration)
            raise ArgumentError, 'Media factory must return a media configuration'
          end
          @mutex.synchronize { raise RuntimeError, 'Integration session closed' if @closed }
          if !profile.equal?(UNSET) || proxy
            params = { 'browserContextId' => context.id }
            params['profile'] = Generated.to_wire(profile) unless profile.equal?(UNSET)
            params['proxy'] = Generated.to_wire(proxy) if proxy
            params['media'] = Generated.to_wire(media) if media
            params['resourcePolicy'] = Generated.to_wire(resource_policy) if resource_policy
            mimic.configure_context(params)
          else
            mimic.set_media_profile(Generated.to_wire(media).merge('browserContextId' => context.id)) if media
            mimic.update_resource_policy({ 'browserContextId' => context.id, 'policy' => Generated.to_wire(resource_policy) }) if resource_policy
          end
          context
        rescue StandardError
          begin
            context.dispose
            @mutex.synchronize { @contexts.delete(context) }
          rescue StandardError
            # Preserve the configuration error; close retains ownership and
            # retries disposal if cleanup could not be confirmed.
          end
          raise
        end
      end

      # @param page [::Ferrum::Page]
      # @return [MimicSDK::PageClient] cached until detached; close releases only this attachment
      def for_page(page)
        raise ArgumentError, 'Expected an actual Ferrum::Page' unless page.is_a?(::Ferrum::Page)
        loop do
          client = @mutex.synchronize do
            raise RuntimeError, 'Integration session closed' if @closed
            @page_clients[page.target_id] ||= begin
              session = @transport.call_raw('Target.attachToTarget', { 'targetId' => page.target_id, 'flatten' => true })
              PageClient.new(@transport, session_id: session.fetch('sessionId'), detach: method(:detach_page_client))
            end
          end
          return client unless client.closed?
          # Never replace a closing handle before its cleanup finishes. Waiting
          # outside the Session mutex allows detach to release its ownership.
          client.close
        end
      end

      def detach_page_client(client)
        @transport.call_raw('Target.detachFromTarget', { 'sessionId' => client.session_id })
      rescue ProtocolError => error
        # The target can close between local invalidation and the detach command.
        raise unless error.code == -32000 && error.message == 'No session with given id'
      ensure
        @mutex.synchronize { @page_clients.delete_if { |_target, owned| owned.equal?(client) } }
      end
      private :detach_page_client

      def close
        return unless @mutex
        contexts, clients = @mutex.synchronize do
          @close_changed.wait(@mutex) while @closing
          if @closed
            raise @close_error if @close_error
            return
          end
          @closed = true
          @closing = true
          owned = [@contexts, @page_clients.values]
          @contexts = []
          @page_clients = {}
          owned
        end
        failure = nil
        cleanup = lambda do |&operation|
          operation.call
        rescue StandardError => error
          failure ||= error
        end
        begin
          clients.each { |client| cleanup.call { client.close } }
          contexts.each { |context| cleanup.call { context.dispose } }
          cleanup.call { @browser&.quit } # A remote Ferrum browser owns no process.
          cleanup.call { @runtime ? @runtime.close : @transport&.close }
        ensure
          clients.each(&:invalidate)
          cleanup.call { @unsubscribe&.call }
          cleanup.call { @attachment_events&.join unless @attachment_events == Thread.current }
          @mutex.synchronize do
            @close_error = failure
            @closing = false
            @close_changed.broadcast
          end
        end
        raise failure if failure
        nil
      end
    end

    # @param runtime_version [String, nil]
    # @param lock_file [String, nil]
    # @param executable_path [String, nil]
    # @param runtime_dir [String, nil]
    # @param archive_path [String, nil]
    # @param allow_download [Boolean] omitted to use the environment default
    # @param startup_timeout [Numeric] seconds
    # @param lock_timeout [Numeric] seconds
    # @param cancelled [Proc, nil] returns true to cancel
    # @param timeout [Numeric] native client timeout in seconds
    # @yield [session] closes the session after the block
    # @yieldparam session [MimicSDK::Ferrum::Session]
    # @return [MimicSDK::Ferrum::Session] when called without a block;
    #   the block form returns the block's result instead
    def self.launch(runtime_version: nil, lock_file: nil, executable_path: nil,
                    runtime_dir: nil, archive_path: nil, allow_download: UNSET,
                    startup_timeout: 30, lock_timeout: 120, cancelled: nil,
                    timeout: 30)
      process = RuntimeManager.new(
        runtime_version: runtime_version, lock_file: lock_file,
        executable_path: executable_path, runtime_dir: runtime_dir,
        archive_path: archive_path, allow_download: allow_download,
        startup_timeout: startup_timeout, lock_timeout: lock_timeout,
        cancelled: cancelled
      ).launch
      session = Session.new(process.endpoint, runtime: process, timeout: timeout)
      return session unless block_given?
      begin
        yield session
      ensure
        session.close
      end
    rescue StandardError
      process&.close
      raise
    end

    # @param endpoint [String] HTTP or browser WebSocket endpoint
    # @param timeout [Numeric] native client timeout in seconds
    # @yield [session] closes the session after the block
    # @yieldparam session [MimicSDK::Ferrum::Session]
    # @return [MimicSDK::Ferrum::Session] when called without a block;
    #   the block form returns the block's result instead
    def self.connect(endpoint, timeout: 30)
      session = Session.new(endpoint, timeout: timeout)
      return session unless block_given?
      begin
        yield session
      ensure
        session.close
      end
    end
  end
end
