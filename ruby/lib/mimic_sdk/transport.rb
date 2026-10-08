require 'json'
require 'net/http'
require 'openssl'
require 'socket'
require 'uri'
require 'timeout'
require 'websocket/driver'

module MimicSDK
  UNSET = Generated::UNSET

  class ProtocolError < StandardError
    attr_reader :code, :data
    def initialize(envelope)
      @code = envelope.fetch('code')
      @data = envelope.fetch('data', UNSET)
      super(envelope.fetch('message'))
    end
  end

  class Transport
    attr_reader :url

    def self.discover(endpoint, timeout: 15)
      uri = URI(endpoint)
      return endpoint if %w[ws wss].include?(uri.scheme) && uri.host
      raise ArgumentError, 'Expected an HTTP or WebSocket endpoint' unless %w[http https].include?(uri.scheme) && uri.host
      uri.path = uri.path.sub(%r{/$}, '') + '/json/version'
      uri.query = nil
      http = Net::HTTP.new(uri.host, uri.port)
      http.use_ssl = uri.scheme == 'https'
      http.open_timeout = http.read_timeout = timeout
      response = http.get(uri.request_uri)
      raise IOError, "CDP discovery HTTP #{response.code}" unless response.is_a?(Net::HTTPSuccess)
      result = JSON.parse(response.body).fetch('webSocketDebuggerUrl')
      parsed = URI(result)
      raise IOError, 'Invalid CDP discovery websocket' unless %w[ws wss].include?(parsed.scheme) && parsed.host
      result
    end

    def initialize(endpoint, timeout: 15)
      @url = self.class.discover(endpoint, timeout: timeout)
      uri = URI(@url)
      @mutex = Mutex.new
      @send_mutex = Mutex.new
      @write_mutex = Mutex.new
      @changed = ConditionVariable.new
      @pending = {}
      @subscriptions = []
      @next_id = 0
      @socket = Socket.tcp(uri.host, uri.port, connect_timeout: timeout)
      @write_timeout = timeout
      if uri.scheme == 'wss'
        tls = OpenSSL::SSL::SSLContext.new
        tls.set_params(verify_mode: OpenSSL::SSL::VERIFY_PEER)
        @socket = OpenSSL::SSL::SSLSocket.new(@socket, tls)
        @socket.sync_close = true
        @socket.hostname = uri.host
        Timeout.timeout(timeout, IOError, 'CDP TLS handshake timed out') { @socket.connect }
        @socket.post_connection_check(uri.host)
      end
      @driver = WebSocket::Driver.client(self, max_length: 64 * 1024 * 1024)
      @driver.on(:open) { @mutex.synchronize { @opened = true; @changed.broadcast } }
      @driver.on(:message) { |event| receive(JSON.parse(event.data)) }
      @driver.on(:error) { |error| fail_connection(IOError.new(error.message)) }
      @driver.on(:close) { |_event| fail_connection(IOError.new('CDP connection closed')) }
      @driver.start
      @reader = Thread.new do
        begin
          loop { @driver.parse(@socket.readpartial(16_384)) }
        rescue StandardError => error
          fail_connection(error)
        end
      end
      deadline = monotonic + timeout
      @mutex.synchronize do
        until @opened || @failure
          remaining = deadline - monotonic
          raise IOError, 'CDP handshake timed out' unless remaining.positive?
          @changed.wait(@mutex, remaining)
        end
        raise @failure if @failure
      end
    rescue StandardError
      close
      raise
    end

    def write(bytes)
      context = Thread.current[:mimic_sdk_write_context]
      _, deadline, cancelled = context if context && context[0].equal?(self)
      deadline ||= monotonic + @write_timeout
      acquire_mutex(@write_mutex, deadline, cancelled)
      begin
        offset = 0
        while offset < bytes.bytesize
          check_deadline(deadline, cancelled)
          written = @socket.write_nonblock(bytes.byteslice(offset..), exception: false)
          case written
          when :wait_readable
            IO.select([@socket], nil, nil, [deadline - monotonic, 0.05].min.clamp(0, 0.05))
          when :wait_writable
            IO.select(nil, [@socket], nil, [deadline - monotonic, 0.05].min.clamp(0, 0.05))
          else
            offset += written
          end
        end
        offset
      ensure
        @write_mutex.unlock
      end
    end

    def call_raw(method, params = UNSET, session_id: nil, timeout: 30, cancelled: nil)
      raise ArgumentError, 'Expected a qualified CDP method' unless method.is_a?(String) && method.match?(/\A[^.\s]+\.[^\s]+\z/)
      raise IOError, 'Call cancelled before dispatch' if cancelled&.call
      deadline = monotonic + timeout
      id = @mutex.synchronize do
        raise @failure if @failure
        @next_id += 1
        @pending[@next_id] = { session_id: session_id, response: nil }
        @next_id
      end
      envelope = { 'id' => id, 'method' => method }
      envelope['params'] = params unless params.equal?(UNSET)
      envelope['sessionId'] = session_id if session_id
      encoded = JSON.generate(envelope, allow_nan: false)
      acquire_mutex(@send_mutex, deadline, cancelled)
      previous_write_context = Thread.current[:mimic_sdk_write_context]
      begin
        Thread.current[:mimic_sdk_write_context] = [self, deadline, cancelled]
        @driver.text(encoded)
      rescue StandardError => error
        fail_connection(error) # A partially written frame cannot be reused.
        raise
      ensure
        Thread.current[:mimic_sdk_write_context] = previous_write_context
        @send_mutex.unlock
      end
      response = @mutex.synchronize do
        loop do
          raise @failure if @failure
          break @pending[id][:response] if @pending[id][:response]
          raise IOError, 'Call cancelled after dispatch; rollback is not implied' if cancelled&.call
          remaining = deadline - monotonic
          raise IOError, 'CDP call timed out; rollback is not implied' unless remaining.positive?
          @changed.wait(@mutex, [remaining, 0.05].min)
        end
      end
      raise IOError, 'CDP response session does not match the request' unless response['sessionId'] == session_id
      raise ProtocolError.new(response['error']) if response.key?('error')
      response.fetch('result')
    ensure
      @mutex&.synchronize { @pending.delete(id) } if id
    end

    def subscribe
      queue = Queue.new
      @mutex.synchronize { raise @failure if @failure; @subscriptions << queue }
      [queue, -> { @mutex.synchronize { @subscriptions.delete(queue) }; queue.close }]
    end

    def close
      return if @closed
      @closed = true
      fail_connection(IOError.new('CDP connection closed'))
      @socket&.close rescue nil
      @reader&.join(1) unless @reader == Thread.current
      nil
    end

    private

    def monotonic = Process.clock_gettime(Process::CLOCK_MONOTONIC)

    def check_deadline(deadline, cancelled)
      raise IOError, 'Call cancelled during dispatch; rollback is not implied' if cancelled&.call
      raise IOError, 'CDP write timed out; rollback is not implied' unless monotonic < deadline
    end

    def acquire_mutex(mutex, deadline, cancelled)
      until mutex.try_lock
        check_deadline(deadline, cancelled)
        sleep 0.005
      end
    end

    def receive(message)
      @mutex.synchronize do
        if message.key?('id')
          @pending[message['id']][:response] = message if @pending.key?(message['id'])
          @changed.broadcast
        elsif message.key?('method')
          @subscriptions.each do |queue|
            raise IOError, 'CDP event subscriber overflow' if queue.length >= 1024
            queue << message
          end
        end
      end
    end

    def fail_connection(error)
      @mutex&.synchronize do
        @failure ||= error
        @changed.broadcast
        @subscriptions.each(&:close)
        @subscriptions.clear
      end
      @socket&.close unless @socket&.closed?
    rescue IOError
      nil
    end
  end

  class Experimental
    def initialize(sender) = (@sender = sender)
    def call(name, params = UNSET, **options)
      raise ArgumentError, 'Expected an exact unqualified wire command leaf' unless name.is_a?(String) && name.match?(/\A[^.\s]+\z/)
      @sender.call("Mimic.#{name}", params, **options)
    end
    def method_missing(name, *args, **options)
      return super if name.to_s.start_with?('_') || %i[to_ary to_hash to_json inspect].include?(name)
      raise ArgumentError, 'Expected at most one params value' if args.length > 1
      call(name.to_s, args.empty? ? UNSET : args.first, **options)
    end
    def respond_to_missing?(_name, _private = false) = false
  end

  class Client < Generated::MimicCommands
    attr_reader :experimental
    def initialize(transport, session_id: nil)
      @transport, @session_id = transport, session_id
      super(self)
      @experimental = Experimental.new(self)
    end
    def call(method, params = UNSET, **options)
      @transport.call_raw(method, params, session_id: @session_id, **options)
    end
  end
end
