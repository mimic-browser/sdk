require 'mimic_sdk'

begin
  require 'ferrum'
rescue LoadError => error
  raise LoadError, "Install the selected adapter dependency with `gem install ferrum`: #{error.message}"
end

module MimicSDK
  module Ferrum
    ContextSetup = Struct.new(:browser_context_id, :mimic, keyword_init: true)

    class Session
      attr_reader :browser, :mimic, :runtime

      def initialize(endpoint, runtime: nil, timeout: 30)
        @runtime = runtime
        @contexts = []
        @mutex = Mutex.new
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
      rescue StandardError
        close
        raise
      end

      # Returns Ferrum::Context, not an SDK automation wrapper. All configuration
      # is complete before a user Page can be created through the returned object.
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

      def for_page(page)
        raise ArgumentError, 'Expected an actual Ferrum::Page' unless page.is_a?(::Ferrum::Page)
        session = @transport.call_raw('Target.attachToTarget', { 'targetId' => page.target_id, 'flatten' => true })
        Client.new(@transport, session_id: session.fetch('sessionId'))
      end

      def close
        return unless @mutex
        @mutex.synchronize do
          return if @closed
          @closed = true
          begin
            @contexts.each(&:dispose)
          ensure
            begin
              @browser&.quit # Remote Ferrum owns no process; quit only disconnects.
            ensure
              @runtime ? @runtime.close : @transport&.close
            end
          end
        end
        nil
      end
    end

    def self.launch(**options)
      process = RuntimeManager.new(**options).launch
      session = Session.new(process.endpoint, runtime: process)
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

    def self.connect(endpoint, **options)
      session = Session.new(endpoint, **options)
      return session unless block_given?
      begin
        yield session
      ensure
        session.close
      end
    end
  end
end
