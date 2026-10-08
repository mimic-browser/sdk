require 'minitest/autorun'
require 'tmpdir'
require 'mimic_sdk'

class SDKTest < Minitest::Test
  def test_exact_selectors_and_manifest_integrity
    manager = MimicSDK::RuntimeManager.new
    lock = manager.resolve_lock
    bundled = JSON.parse(File.read(File.expand_path('../lib/mimic_sdk/runtime-lock.json', __dir__)))
    assert_equal bundled['release'], lock['release']
    %w[latest 1.2 ../v0.2.2 0.2.2+build].each { |version| assert_raises(MimicSDK::RuntimeError) { manager.normalize_version(version) } }
    lock['manifestJson'] += ' '
    assert_raises(MimicSDK::RuntimeError) { manager.validate_lock(lock) }
  end

  def test_immutable_manifest_and_owner_token
    Dir.mktmpdir do |root|
      manager = MimicSDK::RuntimeManager.new(runtime_dir: root)
      lock = manager.resolve_lock
      path = File.join(root, 'manifest.json')
      manager.send(:publish_manifest, path, lock)
      manager.send(:publish_manifest, path, lock)
      changed = Marshal.load(Marshal.dump(lock))
      changed['manifestJson'] += ' '
      changed['manifestSha256'] = Digest::SHA256.hexdigest(changed['manifestJson'])
      assert_raises(MimicSDK::RuntimeError) { manager.send(:publish_manifest, path, changed) }
      owner = File.join(root, '.locks', 'v0.2.2-linux-amd64.lock', 'owner.json')
      manager.with_install_lock('v0.2.2', 'linux-amd64') { File.write(owner, JSON.generate({ 'token' => 'replacement-owner' })) }
      assert File.file?(owner), 'Released another owner lock'
    end
  end

  def test_https_proxy_is_used_without_direct_fallback
    skip 'Listener test runs in WSL' if Gem.win_platform?
    keys = %w[https_proxy HTTPS_PROXY http_proxy HTTP_PROXY no_proxy NO_PROXY]
    saved = keys.to_h { |key| [key, ENV[key]] }
    keys.each { |key| ENV.delete(key) }
    server = TCPServer.new('127.0.0.1', 0)
    ENV['https_proxy'] = "http://127.0.0.1:#{server.addr[1]}"
    request = Queue.new
    worker = Thread.new do
      connection = server.accept
      request << connection.gets
      connection.write("HTTP/1.1 502 Fixture rejection\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
      connection.close
    end
    assert_raises(Net::HTTPFatalError) { MimicSDK::RuntimeManager.new.download('https://github.com/mimic-browser/runtime/releases/download/v0.2.2/release-manifest.json', limit: 1024) }
    assert_equal "CONNECT github.com:443 HTTP/1.1\r\n", request.pop
  ensure
    server&.close
    worker&.join(2)
    saved&.each { |key, value| value ? ENV[key] = value : ENV.delete(key) }
  end

  def test_experimental_is_not_a_schema_allowlist
    calls = []
    sender = Object.new
    sender.define_singleton_method(:call) { |*args, **options| calls << [args, options]; { 'new' => true } }
    proxy = MimicSDK::Experimental.new(sender)
    assert_equal({ 'new' => true }, proxy.newFutureFeature({ 'x' => nil }))
    proxy.call('class', nil)
    proxy.call('omitted')
    assert_equal 'Mimic.newFutureFeature', calls[0][0][0]
    assert_nil calls[1][0][1]
    assert_same MimicSDK::UNSET, calls[2][0][1]
    proxy.inspect
    assert_equal 3, calls.length
    assert_raises(ArgumentError) { proxy.call('Other.namespace', {}) }
  end

  def test_generated_shared_corpus
    cases = JSON.parse(File.binread(File.expand_path('../../conformance/fixtures/wire.json', __dir__)))
    cases = cases.fetch('cases', cases) if cases.is_a?(Hash)
    cases.each do |row|
      next unless row['valid'] != false && row['model']
      klass = MimicSDK::Generated.const_get(row.fetch('model'))
      expected = row.fetch('wire')
      assert_equal expected, klass.from_wire(expected).to_wire, row['name']
    end
  end

  def test_offline_install_and_cross_language_receipt
    archive = ENV['MIMIC_SDK_TEST_ARCHIVE']
    skip 'Set an exact official archive to verify installation' unless archive
    Dir.mktmpdir do |root|
      manager = MimicSDK::RuntimeManager.new(runtime_dir: root, archive_path: archive, allow_download: false)
      first = manager.install
      cached = MimicSDK::RuntimeManager.new(runtime_dir: root, allow_download: false).install
      assert_equal first, cached
      File.open(first['path'], 'r+b') { |file| file.write('corrupt') }
      assert_raises(MimicSDK::RuntimeError) { manager.install }
    end
  end

  def test_cancelled_startup_is_bounded_and_reaped
    skip 'Process test runs inside WSL' if Gem.win_platform?
    Dir.mktmpdir do |root|
      executable = File.join(root, 'slow-runtime')
      File.write(executable, "#!/bin/sh\nexec sleep 60\n")
      File.chmod(0o700, executable)
      start = Process.clock_gettime(Process::CLOCK_MONOTONIC)
      cancelled = -> { Process.clock_gettime(Process::CLOCK_MONOTONIC) - start > 0.02 }
      error = assert_raises(MimicSDK::RuntimeError) do
        MimicSDK::RuntimeManager.new(executable_path: executable, cancelled: cancelled, startup_timeout: 10).launch
      end
      assert_includes error.message, 'cancelled'
      assert_operator Process.clock_gettime(Process::CLOCK_MONOTONIC) - start, :<, 5
    end
  end
end
