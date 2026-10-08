require 'digest'
require 'fileutils'
require 'json'
require 'net/http'
require 'open3'
require 'rubygems/package'
require 'securerandom'
require 'socket'
require 'time'
require 'zlib'
require 'zip'

module MimicSDK
  class RuntimeError < StandardError; end

  class RuntimeManager
    attr_reader :options
    VERSION_PATTERN = /\Av?\d+\.\d+\.\d+(?:-beta\.\d+)?\z/
    SHA_PATTERN = /\A[0-9a-f]{64}\z/

    # @param cancelled [Proc, nil] returns true to cancel a pending operation
    def initialize(runtime_version: nil, lock_file: nil, executable_path: nil,
                   runtime_dir: nil, archive_path: nil, allow_download: UNSET,
                   startup_timeout: 30, lock_timeout: 120, cancelled: nil)
      @options = {
        runtime_version: runtime_version, lock_file: lock_file,
        executable_path: executable_path, runtime_dir: runtime_dir,
        archive_path: archive_path, startup_timeout: startup_timeout,
        lock_timeout: lock_timeout, cancelled: cancelled
      }
      @options[:allow_download] = allow_download unless allow_download.equal?(UNSET)
    end

    def normalize_version(value)
      raise RuntimeError, "Expected an exact runtime version: #{value.inspect}" unless value.is_a?(String) && VERSION_PATTERN.match?(value)
      'v' + value.delete_prefix('v')
    end

    def root
      File.expand_path(options[:runtime_dir] || ENV['MIMIC_RUNTIME_DIR'] ||
        if Gem.win_platform?
          File.join(ENV.fetch('LOCALAPPDATA'), 'Mimic', 'runtimes')
        else
          File.join(ENV['XDG_CACHE_HOME'] || File.join(Dir.home, '.cache'), 'Mimic', 'runtimes')
        end)
    end

    def platform
      cpu = RbConfig::CONFIG['host_cpu']
      raise RuntimeError, "Unsupported runtime CPU: #{cpu}" unless %w[x86_64 amd64].include?(cpu)
      return 'windows-amd64' if Gem.win_platform?
      raise RuntimeError, 'Only Windows x64 and Linux glibc x64 are packaged' unless RUBY_PLATFORM.include?('linux')
      version, status = Open3.capture2('getconf', 'GNU_LIBC_VERSION')
      match = version.match(/\Aglibc (\d+)\.(\d+)/)
      raise RuntimeError, 'Linux runtime requires glibc 2.39+' unless status.success? && match && ([match[1].to_i, match[2].to_i] <=> [2, 39]) >= 0
      'linux-amd64'
    end

    def downloads?
      options.fetch(:allow_download) { ENV['MIMIC_DOWNLOAD'] != '0' }
    end

    def validate_lock(lock)
      version = normalize_version(lock.fetch('release'))
      raise RuntimeError, 'Lock release must be normalized' unless version == lock['release']
      bytes = lock.fetch('manifestJson')
      raise RuntimeError, 'Manifest integrity mismatch' unless SHA_PATTERN.match?(lock.fetch('manifestSha256')) && Digest::SHA256.hexdigest(bytes) == lock['manifestSha256']
      manifest = JSON.parse(bytes)
      raise RuntimeError, 'Manifest bytes and parsed identity differ' unless manifest == lock.fetch('manifest')
      raise RuntimeError, 'Manifest release/source mismatch' unless manifest['version'] == version && manifest['sourceRevision'].is_a?(String) && manifest['sourceRevision'].match?(/\A[0-9a-f]{40}\z/)
      raise RuntimeError, 'Lock must use the exact official release URL' unless lock['baseUrl'] == "https://github.com/mimic-browser/runtime/releases/download/#{version}"
      platforms = []
      manifest.fetch('artifacts').each do |artifact|
        host = artifact.fetch('platform')
        raise RuntimeError, 'Invalid manifest platform' unless %w[windows-amd64 linux-amd64].include?(host) && !platforms.include?(host)
        platforms << host
        suffix = host.start_with?('windows') ? '.zip' : '.tar.gz'
        valid = artifact['archive'] == "mimic-#{version}-#{host}#{suffix}" && artifact['binaryVersion'] == version &&
          SHA_PATTERN.match?(artifact.fetch('sha256')) && SHA_PATTERN.match?(artifact.fetch('binarySha256')) &&
          artifact['size'].is_a?(Integer) && artifact['size'].positive? && artifact['size'] <= 1024**3
        raise RuntimeError, 'Invalid artifact identity' unless valid
      end
      raise RuntimeError, 'Empty artifact manifest' if platforms.empty?
      lock
    rescue KeyError, JSON::ParserError, TypeError => error
      raise RuntimeError, "Invalid runtime lock: #{error.message}"
    end

    def resolve_lock
      explicit = options[:lock_file] && validate_lock(JSON.parse(File.binread(options[:lock_file])))
      version = options[:runtime_version] && normalize_version(options[:runtime_version])
      raise RuntimeError, 'Explicit version and lock conflict' if explicit && version && explicit['release'] != version
      return explicit if explicit
      version ||= normalize_version(ENV['MIMIC_RUNTIME_VERSION']) if ENV['MIMIC_RUNTIME_VERSION']
      default = validate_lock(JSON.parse(File.binread(File.join(__dir__, 'runtime-lock.json'))))
      return default if !version || version == default['release']
      cached = File.join(root, '.manifests', "#{version}.json")
      if File.file?(cached)
        lock = validate_lock(JSON.parse(File.binread(cached)))
        raise RuntimeError, 'Cached release mismatch' unless lock['release'] == version
        return lock
      end
      base = "https://github.com/mimic-browser/runtime/releases/download/#{version}"
      bytes = download("#{base}/release-manifest.json", limit: 4 * 1024**2)
      sums = download("#{base}/SHA256SUMS", limit: 1024**2)
      sha = Digest::SHA256.hexdigest(bytes)
      raise RuntimeError, 'Manifest checksum list mismatch' unless sums.lines.any? { |line| line.strip == "#{sha}  release-manifest.json" }
      lock = validate_lock({ 'release' => version, 'manifestJson' => bytes, 'manifest' => JSON.parse(bytes), 'manifestSha256' => sha, 'baseUrl' => base })
      FileUtils.mkdir_p(File.dirname(cached))
      publish_manifest(cached, lock)
      lock
    end

    def download(url, limit:, redirects: 5)
      raise RuntimeError, 'Downloads disabled; preinstall the exact runtime or supply its verified archive' unless downloads?
      uri = URI(url)
      raise RuntimeError, 'Runtime downloads require HTTPS' unless uri.scheme == 'https'
      proxy = uri.find_proxy
      http = Net::HTTP.new(uri.host, uri.port, proxy&.host, proxy&.port,
        proxy&.user && URI.decode_www_form_component(proxy.user),
        proxy&.password && URI.decode_www_form_component(proxy.password))
      http.use_ssl = true
      http.open_timeout = 20
      http.read_timeout = 120
      body = String.new(encoding: Encoding::BINARY)
      response = http.start do |connection|
        connection.request_get(uri.request_uri) do |result|
          if result.is_a?(Net::HTTPSuccess)
            result.read_body do |chunk|
              check_cancelled
              raise RuntimeError, 'Download exceeds declared size' if body.bytesize + chunk.bytesize > limit
              body << chunk
            end
          end
          result
        end
      end
      if response.is_a?(Net::HTTPRedirection)
        raise RuntimeError, 'Too many download redirects' unless redirects.positive?
        return download(URI.join(url, response.fetch('location')).to_s, limit: limit, redirects: redirects - 1)
      end
      raise RuntimeError, "Runtime download HTTP #{response.code}" unless response.is_a?(Net::HTTPSuccess)
      body
    end

    def with_install_lock(release, host)
      parent = File.join(root, '.locks')
      FileUtils.mkdir_p(parent)
      directory = File.join(parent, "#{release}-#{host}.lock")
      deadline = monotonic + options.fetch(:lock_timeout, 120)
      loop do
        check_cancelled
        begin
          Dir.mkdir(directory)
          break
        rescue Errno::EEXIST
          raise RuntimeError, "Installation lock timed out: #{directory}; inspect owner before stale-lock repair" if monotonic >= deadline
          sleep 0.05
        end
      end
      begin
        owner_token = SecureRandom.uuid
        atomic_json(File.join(directory, 'owner.json'), { 'pid' => Process.pid, 'hostname' => Socket.gethostname, 'token' => owner_token, 'createdAt' => Time.now.utc.iso8601 })
        yield
      ensure
        owner_path = File.join(directory, 'owner.json')
        if File.file?(owner_path) && JSON.parse(File.binread(owner_path))['token'] == owner_token
          File.delete(owner_path)
          Dir.rmdir(directory)
        end
      end
    end

    def install
      check_cancelled
      host = platform
      lock = resolve_lock
      artifact = lock['manifest']['artifacts'].find { |item| item['platform'] == host }
      raise RuntimeError, "No artifact for #{host}" unless artifact
      name = host.start_with?('windows') ? 'mimic.exe' : 'mimic'
      destination = File.join(root, lock['release'], host, artifact['binarySha256'])
      receipt = { 'release' => lock['release'], 'platform' => host, 'sourceRevision' => lock['manifest']['sourceRevision'],
        'archiveSha256' => artifact['sha256'], 'binarySha256' => artifact['binarySha256'],
        'manifestSha256' => lock['manifestSha256'], 'executable' => name }
      with_install_lock(lock['release'], host) do
        if File.exist?(destination)
          verify(destination, receipt)
          return receipt.merge('path' => File.join(destination, name))
        end
        archive = options[:archive_path] ? File.binread(options[:archive_path]) : download("#{lock['baseUrl']}/#{artifact['archive']}", limit: artifact['size'])
        raise RuntimeError, 'Archive integrity mismatch' unless archive.bytesize == artifact['size'] && Digest::SHA256.hexdigest(archive) == artifact['sha256']
        staging = File.join(root, '.staging', SecureRandom.uuid)
        FileUtils.mkdir_p(staging)
        begin
          extract(archive, artifact['archive'], staging, "mimic-#{lock['release']}-#{host}")
          binary = File.join(staging, name)
          raise RuntimeError, 'Executable integrity mismatch' unless File.file?(binary) && Digest::SHA256.file(binary).hexdigest == artifact['binarySha256']
          File.chmod(0o700, binary)
          atomic_json(File.join(staging, 'installation.json'), receipt)
          FileUtils.mkdir_p(File.dirname(destination))
          File.rename(staging, destination)
          FileUtils.mkdir_p(File.join(root, '.manifests'))
          publish_manifest(File.join(root, '.manifests', "#{lock['release']}.json"), lock)
        ensure
          FileUtils.rm_rf(staging)
        end
      end
      receipt.merge('path' => File.join(destination, name))
    end

    def verify(directory, expected = nil)
      receipt = JSON.parse(File.binread(File.join(directory, 'installation.json')))
      expected&.each { |key, value| raise RuntimeError, 'Cached provenance mismatch' unless receipt[key] == value }
      executable = receipt.fetch('executable')
      raise RuntimeError, 'Invalid executable receipt path' unless %w[mimic mimic.exe].include?(executable)
      binary = File.join(directory, executable)
      raise RuntimeError, 'Cached executable is not a regular file' unless File.file?(binary) && !File.symlink?(binary)
      raise RuntimeError, 'Cached executable hash mismatch' unless Digest::SHA256.file(binary).hexdigest == receipt.fetch('binarySha256')
      receipt.merge('path' => binary)
    rescue Errno::ENOENT, JSON::ParserError, KeyError => error
      raise RuntimeError, "Incomplete cached installation: #{error.message}"
    end

    def list
      Dir.glob(File.join(root, 'v*', '*-amd64', '*', 'installation.json')).map { |file| verify(File.dirname(file)) }
    end

    def prune(installation)
      directory = File.dirname(File.expand_path(installation.fetch('path')))
      expected = File.join(root, installation.fetch('release'), installation.fetch('platform'), installation.fetch('binarySha256'))
      raise RuntimeError, 'Prune target escapes the installation root' unless directory == expected
      resolved_root = File.realpath(root)
      resolved_directory = File.realpath(directory)
      raise RuntimeError, 'Prune target traverses a symbolic link' unless resolved_directory == File.join(resolved_root, installation['release'], installation['platform'], installation['binarySha256'])
      with_install_lock(installation['release'], installation['platform']) do
        verify(directory, installation.reject { |key, _| key == 'path' })
        Dir.glob(File.join(directory, '.leases', '*.json')).each do |file|
          lease = JSON.parse(File.binread(file))
          raise RuntimeError, 'Foreign or incomplete runtime lease' unless lease['hostname'] == Socket.gethostname && lease['runtimePid'].is_a?(Integer) && lease['runtimePid'].positive?
          begin
            Process.kill(0, lease['runtimePid'])
            raise RuntimeError, 'Cannot prune a running runtime'
          rescue Errno::ESRCH
            # Confirmed dead local process; this retained lease is stale.
          end
        end
        FileUtils.rm_rf(directory)
      end
      nil
    end

    def launch
      check_cancelled
      platform
      explicit = options[:executable_path] || ENV['MIMIC_EXECUTABLE_PATH']
      if explicit
        path = File.expand_path(explicit)
        raise RuntimeError, "Invalid explicit executable: #{path}" unless File.file?(path) && File.executable?(path)
        lock = options[:lock_file] && resolve_lock
        if lock
          artifact = lock['manifest']['artifacts'].find { |item| item['platform'] == platform }
          raise RuntimeError, 'Explicit executable does not match lock' unless artifact && Digest::SHA256.file(path).hexdigest == artifact['binarySha256']
        end
        expected = lock ? lock['release'] : options[:runtime_version] || ENV['MIMIC_RUNTIME_VERSION']
        return RuntimeProcess.new(path, expected_version: expected && normalize_version(expected), timeout: options.fetch(:startup_timeout, 30), cancelled: options[:cancelled])
      end
      installation = install
      with_install_lock(installation['release'], installation['platform']) do
        verify(File.dirname(installation['path']), installation.reject { |key, _| key == 'path' })
        RuntimeProcess.new(installation['path'], expected_version: installation['release'], timeout: options.fetch(:startup_timeout, 30), cancelled: options[:cancelled], lease_directory: File.join(File.dirname(installation['path']), '.leases'))
      end
    end

    private

    def check_cancelled
      raise RuntimeError, 'Runtime operation cancelled' if options[:cancelled]&.call
    end

    def monotonic = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    def publish_manifest(path, value)
      temporary = path + '.' + SecureRandom.uuid + '.tmp'
      File.binwrite(temporary, JSON.generate(value))
      begin
        File.link(temporary, path)
      rescue Errno::EEXIST
        existing = validate_lock(JSON.parse(File.binread(path)))
        raise RuntimeError, 'Conflicting immutable release manifest' unless existing['release'] == value['release'] && existing['manifestSha256'] == value['manifestSha256']
      end
    ensure
      File.delete(temporary) if temporary && File.exist?(temporary)
    end

    def atomic_json(path, value)
      temporary = path + '.' + SecureRandom.uuid + '.tmp'
      File.binwrite(temporary, JSON.generate(value))
      File.rename(temporary, path)
    ensure
      File.delete(temporary) if temporary && File.exist?(temporary)
    end

    def extract(bytes, name, directory, prefix)
      seen = {}
      total = 0
      write = lambda do |entry_name, size, type, data|
        raise RuntimeError, 'Unsafe archive path' if entry_name.start_with?('/') || entry_name.include?('\\') || entry_name.include?(':') || entry_name.split('/').any? { |part| %w[. ..].include?(part) }
        next if entry_name == prefix + '/' && type == :directory
        raise RuntimeError, 'Unexpected archive root' unless entry_name.start_with?(prefix + '/')
        relative = entry_name.delete_prefix(prefix + '/').delete_suffix('/')
        raise RuntimeError, 'Duplicate archive entry' if relative.empty? || seen[relative]
        seen[relative] = true
        output = File.join(directory, relative)
        if type == :directory
          FileUtils.mkdir_p(output)
          next
        end
        raise RuntimeError, 'Archive links/special files are unsupported' unless type == :file
        total += size
        raise RuntimeError, 'Archive extraction size exceeds bound' if size.negative? || total > 2 * 1024**3
        FileUtils.mkdir_p(File.dirname(output))
        File.open(output, 'wx', 0o600) do |file|
          count = IO.copy_stream(data, file, size)
          raise RuntimeError, 'Archive entry length mismatch' unless count == size
        end
      end
      if name.end_with?('.zip')
        Zip::InputStream.open(StringIO.new(bytes)) do |zip|
          while (entry = zip.get_next_entry)
            type = entry.symlink? ? :link : entry.directory? ? :directory : :file
            write.call(entry.name, entry.size, type, zip)
          end
        end
      else
        Zlib::GzipReader.wrap(StringIO.new(bytes)) do |gzip|
          Gem::Package::TarReader.new(gzip) do |tar|
            tar.each { |entry| write.call(entry.full_name, entry.header.size, entry.directory? ? :directory : entry.file? ? :file : :link, entry) }
          end
        end
      end
    end
  end

  class RuntimeProcess
    attr_reader :endpoint, :transport, :mimic, :pid
    def initialize(executable, expected_version: nil, timeout: 30, lease_directory: nil, cancelled: nil)
      @mutex, @close_mutex, @ready = Mutex.new, Mutex.new, ConditionVariable.new
      @log = ''
      stdin, stdout, stderr, @waiter = Open3.popen3(executable, '--browser-mode', 'headless', '--listen', '127.0.0.1:0')
      stdin.close
      @pid = @waiter.pid
      @readers = [stdout, stderr].map do |stream|
        Thread.new do
          stream.each_line do |line|
            @mutex.synchronize do
              @log = (@log + line).byteslice(-16_384, 16_384) || (@log + line)
              if stream == stdout && (match = line.match(%r{\AMimic listening on (http://127\.0\.0\.1:(\d+))\s*\z}))
                @endpoint = match[1] if (1..65_535).cover?(match[2].to_i)
                @ready.broadcast
              end
            end
          end
        ensure
          stream.close
        end
      end
      deadline = Process.clock_gettime(Process::CLOCK_MONOTONIC) + timeout
      @mutex.synchronize do
        until @endpoint
          raise RuntimeError, 'Runtime startup cancelled' if cancelled&.call
          raise RuntimeError, "Runtime exited: #{@log}" unless @waiter.alive?
          remaining = deadline - Process.clock_gettime(Process::CLOCK_MONOTONIC)
          raise RuntimeError, "Runtime startup timed out: #{@log}" unless remaining.positive?
          @ready.wait(@mutex, [remaining, 0.05].min)
        end
      end
      remaining = deadline - Process.clock_gettime(Process::CLOCK_MONOTONIC)
      raise RuntimeError, 'Runtime startup timed out before discovery' unless remaining.positive?
      @transport = Transport.new(@endpoint, timeout: remaining)
      @mimic = Client.new(@transport)
      remaining = deadline - Process.clock_gettime(Process::CLOCK_MONOTONIC)
      raise RuntimeError, 'Runtime startup timed out before identity' unless remaining.positive?
      identity = @transport.call_raw('Mimic.getVersion', {}, timeout: remaining, cancelled: cancelled)
      raise RuntimeError, 'Invalid Mimic runtime identity' if identity['version'].to_s.empty? || identity['chromeVersion'].to_s.empty?
      raise RuntimeError, "Requested #{expected_version}, runtime reports #{identity['version']}" if expected_version && ![expected_version, expected_version.delete_prefix('v')].include?(identity['version'])
      if lease_directory
        FileUtils.mkdir_p(lease_directory)
        @lease = File.join(lease_directory, SecureRandom.uuid + '.json')
        File.binwrite(@lease, JSON.generate({ 'launcherPid' => Process.pid, 'runtimePid' => @pid, 'hostname' => Socket.gethostname, 'createdAt' => Time.now.utc.iso8601 }))
      end
    rescue StandardError
      close
      raise
    end

    def close
      @close_mutex.synchronize do
        return if @closed
        @closed = true
        begin
          @transport&.call_raw('Browser.close', {}, timeout: 2)
        rescue StandardError
          # Disconnect during graceful shutdown is expected; the child wait below
          # remains authoritative and force-stop is bounded to this owned PID.
        ensure
          @transport&.close
        end
        if @waiter&.alive?
          unless @waiter.join(2)
            Process.kill('TERM', @pid) rescue Errno::ESRCH
            unless @waiter.join(2)
              Process.kill('KILL', @pid) rescue Errno::ESRCH
              @waiter.join
            end
          end
        end
        @readers&.each { |reader| reader.join(1) }
        File.delete(@lease) if @lease && File.exist?(@lease)
      end
      nil
    end
  end
end
