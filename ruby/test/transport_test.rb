require 'minitest/autorun'
require 'mimic_sdk'

class RawTransportTest < Minitest::Test
  def setup
    skip 'Listener tests run inside WSL only' if Gem.win_platform?
    @server = TCPServer.new('127.0.0.1', 0)
    @seen = Queue.new
    @resume = Queue.new
    @worker = Thread.new do
      @peer = @server.accept
      driver = WebSocket::Driver.server(@peer)
      driver.on(:connect) { driver.start }
      batch = []
      driver.on(:message) do |event|
        request = JSON.parse(event.data)
        @seen << request
        response = { 'id' => request['id'] }
        response['sessionId'] = request['sessionId'] if request.key?('sessionId')
        case request.fetch('method')
        when 'Mimic.batch'
          batch << request
          if batch.length == 20
            batch.reverse_each { |item| driver.text(JSON.generate({ 'id' => item['id'], 'sessionId' => item['sessionId'], 'result' => item['params'] })) }
          end
          next
        when 'Mimic.futureCommand'
          response['error'] = { 'code' => -32601, 'message' => 'Unknown new command', 'data' => { 'retry' => false, 'detail' => nil } }
        when 'Mimic.never'
          next
        when 'Mimic.drop'
          @peer.close
          next
        else
          response['result'] = request
        end
        response['sessionId'] = 'another-page' if request['method'] == 'Mimic.wrongSession'
        driver.text(JSON.generate(response))
        @resume.pop if request['method'] == 'Mimic.pauseReader'
        if request['method'] == 'Mimic.events'
          3.times { |index| driver.text(JSON.generate({ 'method' => 'Mimic.event', 'sessionId' => 'page', 'params' => { 'index' => index } })) }
        end
      end
      loop { driver.parse(@peer.readpartial(16_384)) }
    rescue EOFError, IOError, Errno::EBADF
      # The test deliberately closes the transport and server.
    end
    @transport = MimicSDK::Transport.new("ws://127.0.0.1:#{@server.addr[1]}")
    @client = MimicSDK::Client.new(@transport, session_id: 'page')
  end

  def teardown
    @resume << true if @resume
    @transport&.close
    @peer&.close unless @peer&.closed?
    @server&.close
    @worker&.join(2)
    refute @worker&.alive?, 'Fixture server did not stop' if @worker
  end

  def test_omission_null_unknown_command_and_exact_session
    missing = @client.experimental.call('echo')
    explicit = @client.experimental.call('echo', nil)
    refute missing.key?('params')
    assert explicit.key?('params')
    assert_nil explicit['params']
    assert_equal 'page', explicit['sessionId']
    error = assert_raises(MimicSDK::ProtocolError) { @client.experimental.futureCommand({ 'value' => 1 }) }
    assert_equal(-32601, error.code)
    assert_equal 'Unknown new command', error.message
    assert_equal({ 'retry' => false, 'detail' => nil }, error.data)
    assert_equal 3, @seen.size, 'No schema discovery or automatic retry is allowed'
  end

  def test_concurrent_reverse_order_and_ordered_events
    threads = 20.times.map do |index|
      Thread.new { @client.experimental.call('batch', { 'index' => index }) }
    end
    assert_equal((0...20).to_a, threads.map(&:value).map { |result| result['index'] })
    queue, unsubscribe = @transport.subscribe
    @client.experimental.call('events', {})
    assert_equal [0, 1, 2], 3.times.map { queue.pop['params']['index'] }
    unsubscribe.call
    assert queue.closed?
  end

  def test_cancellation_timeout_disconnect_and_no_rollback_claim
    assert_raises(IOError) { @client.experimental.call('never', {}, cancelled: -> { true }) }
    assert_equal 0, @seen.size
    error = assert_raises(IOError) { @client.experimental.call('never', {}, timeout: 0.02) }
    assert_includes error.message, 'rollback is not implied'
    cancelled = false
    setter = Thread.new { sleep 0.02; cancelled = true }
    error = assert_raises(IOError) { @client.experimental.call('never', {}, cancelled: -> { cancelled }) }
    setter.join
    assert_includes error.message, 'after dispatch'
    assert_equal 2, @seen.size
    assert_raises(IOError, EOFError) { @client.experimental.call('drop', {}, timeout: 1) }
  end

  def test_cross_session_response_is_not_accepted
    error = assert_raises(IOError) { @client.experimental.call('wrongSession', {}) }
    assert_includes error.message, 'session does not match'
  end

  def test_socket_backpressure_obeys_the_call_deadline
    @client.experimental.call('pauseReader', {})
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    error = assert_raises(IOError) { @client.experimental.call('large', { 'value' => 'x' * (16 * 1024 * 1024) }, timeout: 0.05) }
    assert_includes error.message, 'timed out'
    assert_operator Process.clock_gettime(Process::CLOCK_MONOTONIC) - started, :<, 2
  end

  def test_system_error_after_partial_write_poisons_connection
    socket = @transport.instance_variable_get(:@socket)
    original_write = socket.method(:write_nonblock)
    writes = 0
    socket.define_singleton_method(:write_nonblock) do |bytes, exception: true|
      writes += 1
      raise Errno::EPIPE, 'forced error after partial frame' if writes > 1
      original_write.call(bytes.byteslice(0, 2), exception: exception)
    end
    assert_raises(Errno::EPIPE) { @client.experimental.call('partial', { 'value' => 'payload' }) }
    assert socket.closed?, 'A partially written websocket must never be reused'
    assert_raises(Errno::EPIPE) { @client.experimental.call('retry', {}) }
    assert_equal 2, writes, 'Failed dispatch must not attempt a second frame'
  end
end
