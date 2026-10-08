require 'minitest/autorun'
require 'open3'
require 'timeout'
require 'mimic_sdk/ferrum'

class MediaFactoryTest < Minitest::Test
  def test_private_sources_and_public_identities_in_native_context
    binary = ENV['MIMIC_MEDIA_FIXTURE']
    skip 'Requires explicit synthetic-only media fixture in Linux/WSL' if Gem.win_platform? || !binary
    input, output, errors, waiter = Open3.popen3(binary, '--browser-mode', 'headless', '--listen', '127.0.0.1:0')
    input.close
    diagnostics = Thread.new { errors.read }
    Timeout.timeout(30) do
      endpoint = fixture = nil
      until endpoint && fixture
        line = output.gets or raise 'Synthetic fixture exited before readiness'
        endpoint = line.delete_prefix('Mimic listening on ').strip if line.start_with?('Mimic listening on ')
        fixture = line.delete_prefix('Fixture listening on ').strip if line.start_with?('Fixture listening on ')
      end
      control = MimicSDK::Transport.new(endpoint)
      session = MimicSDK::Ferrum.connect(endpoint)
      before = control.call_raw('Target.getBrowserContexts')
      error = assert_raises(RuntimeError) do
        session.new_context(media: ->(_setup) { raise 'Factory rejected configuration' })
      end
      assert_equal 'Factory rejected configuration', error.message
      assert_equal before, control.call_raw('Target.getBrowserContexts'), 'Failed factory must dispose its Context'
      assert_raises(ArgumentError) { session.new_context(media: ->(_setup) { nil }) }
      closing = MimicSDK::Ferrum.connect(endpoint)
      error = assert_raises(MimicSDK::RuntimeError) do
        closing.new_context(media: ->(_setup) { closing.close; { 'devices' => [] } })
      end
      assert_includes error.message, 'session closed'
      assert_equal before, control.call_raw('Target.getBrowserContexts'), 'Self-close callback must not retain a Context'
      source_id = context_id = nil
      context = session.new_context(media: lambda do |setup|
        # This would deadlock if the adapter called user code under its mutex.
        session.new_context(media: { 'devices' => [] })
        context_id = setup.browser_context_id
        sources = setup.mimic.get_media_sources({ 'browserContextId' => context_id }).sources
        source = lambda do |label|
          selected = sources.find { |item| item.label == label }
          refute_nil selected
          refute_includes selected.source_id, 'native'
          source_id = selected.source_id if selected.kind == 'videoinput'
          { 'sourceId' => selected.source_id }
        end
        {
          'devices' => [
            {
              'key' => 'front', 'kind' => 'videoinput', 'label' => 'Studio Camera', 'group' => 'desk',
              'source' => source.call('Private native camera B'),
              'modes' => [{ 'width' => 16, 'height' => 8, 'frameRate' => 30 }],
              'defaultMode' => { 'width' => 16, 'height' => 8, 'frameRate' => 30 },
              'processing' => { 'resize' => 'crop-and-scale' }
            },
            {
              'key' => 'voice', 'kind' => 'audioinput', 'label' => 'Studio Microphone', 'group' => 'desk',
              'source' => source.call('Private native microphone B')
            }
          ]
        }
      end)
      assert_instance_of Ferrum::Context, context
      assert_equal context_id, context.id
      control.call_raw('Browser.grantPermissions', {
        'browserContextId' => context.id, 'origin' => fixture, 'permissions' => %w[videoCapture audioCapture]
      })
      page = context.create_page
      page.resize(width: 17, height: 19) # A media factory alone leaves ordinary CDP emulation available.
      page.go_to(fixture)
      observed = page.evaluate_async(<<~JS, 10)
        const done = arguments[0];
        (async () => {
          const devices = await navigator.mediaDevices.enumerateDevices();
          const camera = devices.find(item => item.label === 'Studio Camera');
          const microphone = devices.find(item => item.label === 'Studio Microphone');
          const stream = await navigator.mediaDevices.getUserMedia({
            video: {deviceId: {exact: camera.deviceId}},
            audio: {deviceId: {exact: microphone.deviceId}}
          });
          const video = document.createElement('video');
          video.srcObject = stream;
          document.body.append(video);
          await video.play();
          await new Promise(resolve => video.requestVideoFrameCallback(resolve));
          const canvas = document.createElement('canvas');
          canvas.width = 16;
          canvas.height = 8;
          const draw = canvas.getContext('2d');
          draw.drawImage(video, 0, 0, 16, 8);
          const observation = {
            devices: devices.map(item => item.toJSON()),
            pixel: Array.from(draw.getImageData(0, 0, 1, 1).data),
            tracks: stream.getTracks().map(track => ({label: track.label, settings: track.getSettings()}))
          };
          stream.getTracks().forEach(track => track.stop());
          done(observation);
        })().catch(error => done({error: {name: error.name, message: error.message}}));
      JS
      refute observed.key?('error'), observed.inspect
      assert_equal [0, 0, 255, 255], observed.fetch('pixel'), 'Public camera must capture private source B'
      devices = observed.fetch('devices')
      assert_equal ['Studio Camera', 'Studio Microphone'], devices.map { |device| device.fetch('label') }.sort
      assert_equal devices.first.fetch('groupId'), devices.last.fetch('groupId')
      refute_equal devices.first.fetch('deviceId'), devices.last.fetch('deviceId')
      refute_includes JSON.generate(observed), 'Private native'
      refute_includes JSON.generate(observed), source_id
      observed.fetch('tracks').each do |track|
        device = devices.find { |item| item.fetch('label') == track.fetch('label') }
        refute_nil device
        assert_equal device.fetch('deviceId'), track.fetch('settings').fetch('deviceId')
        assert_equal device.fetch('groupId'), track.fetch('settings').fetch('groupId')
      end
    ensure
      session&.close
      closing&.close
      control&.close
    end
  ensure
    if waiter
      Process.kill('TERM', waiter.pid) if waiter.alive?
      waiter.join(3) || (Process.kill('KILL', waiter.pid); waiter.join)
    end
    output&.close
    diagnostics&.join(2)
    errors&.close
  end
end
