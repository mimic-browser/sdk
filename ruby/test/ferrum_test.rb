require 'minitest/autorun'
require 'mimic_sdk/ferrum'

class FerrumIntegrationTest < Minitest::Test
  def test_session_close_joins_an_explicit_detach_already_in_progress
    binary = ENV['MIMIC_SDK_TEST_RUNTIME']
    skip 'Run headless integration in WSL with MIMIC_SDK_TEST_RUNTIME' if Gem.win_platform? || !binary
    session = MimicSDK::Ferrum.launch(executable_path: binary)
    transport = session.instance_variable_get(:@transport)
    page = session.new_context.create_page
    handle = session.for_page(page)
    entered, release = Queue.new, Queue.new
    original = transport.method(:call_raw)
    transport.define_singleton_method(:call_raw) do |method, params = MimicSDK::UNSET, **options|
      if method == 'Target.detachFromTarget' && params['sessionId'] == handle.session_id
        entered << true
        release.pop
      end
      original.call(method, params, **options)
    end
    first = Thread.new { handle.close }
    Timeout.timeout(5) { entered.pop }
    second = Thread.new { handle.close }
    closing = Thread.new { session.close }
    Timeout.timeout(5) { sleep 0.001 until session.instance_variable_get(:@closed) }
    closing_again = Thread.new { session.close }
    assert closing.alive?, 'Session close returned while its explicit detach was still pending'
    assert_nil second.join(0.05), 'Concurrent handle close returned before the shared detach completed'
    assert_nil closing_again.join(0.05), 'Concurrent Session close returned before owned detach completed'
    assert_kind_of String, session.mimic.get_version.version, 'Session closed its transport before detach finished'
    release << true
    Timeout.timeout(5) { [first, second, closing, closing_again].each(&:value) }
    assert handle.closed?
  ensure
    release&.push(true)
    [first, second, closing, closing_again].compact.each { |worker| worker.join(5) }
    session&.close
  end

  def test_page_capability_attachment_is_shared_and_has_explicit_lifetime
    binary = ENV['MIMIC_SDK_TEST_RUNTIME']
    skip 'Run headless integration in WSL with MIMIC_SDK_TEST_RUNTIME' if Gem.win_platform? || !binary
    session = MimicSDK::Ferrum.launch(executable_path: binary)
    begin
      page = session.new_context.create_page
      clients = 8.times.map { Thread.new { session.for_page(page) } }.map(&:value)
      assert_equal 1, clients.map(&:object_id).uniq.length, 'Concurrent callers allocated duplicate attachments'
      handle = clients.first
      assert_equal page.target_id, handle.call('Target.getTargetInfo').fetch('targetInfo').fetch('targetId')
      handle.close
      handle.close
      assert_raises(IOError) { handle.get_status }
      error = assert_raises(MimicSDK::ProtocolError) do
        session.mimic.call('Target.detachFromTarget', { 'sessionId' => handle.session_id })
      end
      assert_equal 'No session with given id', error.message
      assert_equal 42, page.evaluate('21 * 2'), 'Closing a capability handle closed its native Page'
      renewed = session.for_page(page)
      refute_same handle, renewed
      page.close
      begin
        Timeout.timeout(5) { sleep 0.001 until renewed.closed? }
      rescue Timeout::Error
        targets = session.mimic.call('Target.getTargets').fetch('targetInfos')
        remaining = targets.map { |target| target.fetch('targetId') }
        watcher = session.instance_variable_get(:@attachment_events).status
        flunk "Native Page close did not invalidate its capability: " \
              "target=#{page.target_id}, session=#{renewed.session_id}, " \
              "remaining=#{remaining.inspect}, event_thread=#{watcher.inspect}"
      end
      assert_raises(IOError) { renewed.get_status }
      assert_empty session.instance_variable_get(:@page_clients), 'Closed native Page retained its attachment cache entry'
      session.close
      assert_raises(MimicSDK::RuntimeError) { session.for_page(page) }
    ensure
      session.close
    end
  end

  def test_pages_remain_bound_when_auto_attach_session_assignment_is_delayed
    binary = ENV['MIMIC_SDK_TEST_RUNTIME']
    skip 'Run headless integration in WSL with MIMIC_SDK_TEST_RUNTIME' if Gem.win_platform? || !binary
    owner = MimicSDK::Ferrum.launch(executable_path: binary)
    attached = MimicSDK::Ferrum.connect(owner.runtime.endpoint)
    before = owner.mimic.call('Target.getBrowserContexts')
    pending = Queue.new
    begin
      contexts = 2.times.map { attached.new_context }
      contexts.each do |context|
        original = context.method(:add_target)
        # Model a targetCreated notification waking create_page before the
        # subsequent attachedToTarget callback can assign its session ID.
        # Actual attachment and debugger resumption still run normally.
        context.define_singleton_method(:add_target) do |params:, session_id: nil|
          pending << session_id if session_id
          original.call(params: params)
        end
      end
      pages = contexts.map(&:create_page)
      pages.each_with_index do |page, index|
        assert_instance_of Ferrum::Page, page
        info = page.command('Target.getTargetInfo').fetch('targetInfo')
        assert_equal page.target_id, info.fetch('targetId'), 'Commands must address the requested Page'
        assert_equal contexts[index].id, info.fetch('browserContextId')
        page.evaluate("document.body.textContent = 'page #{index}'")
      end
      assert_equal ['page 0', 'page 1'], pages.map { |page| page.evaluate('document.body.textContent') }
      pages.each do |page|
        info = attached.for_page(page).call('Target.getTargetInfo').fetch('targetInfo')
        assert_equal page.target_id, info.fetch('targetId'), 'Mimic commands share the native Page target'
      end
      refute pending.empty?, 'The fixture must withhold a real auto-attach session assignment'
      # Closing must also work before delayed session assignments arrive.
      attached.close
      assert_equal before, owner.mimic.call('Target.getBrowserContexts')
      assert_kind_of String, owner.mimic.get_version.version
    ensure
      begin
        attached.close
      ensure
        owner.close
      end
    end
  end

  def test_native_objects_owned_and_attached_lifecycle
    binary = ENV['MIMIC_SDK_TEST_RUNTIME']
    skip 'Run headless integration in WSL with MIMIC_SDK_TEST_RUNTIME' unless binary
    owner = MimicSDK::Ferrum.launch(executable_path: binary)
    attached = MimicSDK::Ferrum.connect(owner.runtime.endpoint)
    begin
      context = attached.new_context(media: { 'devices' => [] })
      assert_instance_of Ferrum::Context, context
      page = context.create_page
      assert_instance_of Ferrum::Page, page
      page.command('Page.setDocumentContent', frameId: page.main_frame.id, html: '<input id="name"><button onclick="document.querySelector(\'h1\').textContent=document.querySelector(\'input\').value">Save</button><h1>Before</h1>')
      page.at_css('#name').focus
      page.keyboard.type('Ruby SDK')
      page.at_css('button').click
      assert_equal 'Ruby SDK', page.at_css('h1').text
      assert_kind_of Hash, attached.for_page(page).experimental.call('getTrace', {})
      attached.close
      assert_kind_of String, owner.mimic.get_version.version
      if ENV['MIMIC_SDK_TEST_CONFIGURE'] == '1'
        managed = owner.new_context(profile: { 'generate' => { 'seed' => 'ruby-sdk' } })
        managed_page = managed.create_page
        assert_kind_of Numeric, managed_page.evaluate('navigator.hardwareConcurrency')
        assert_raises(Ferrum::BrowserError) { managed_page.resize(width: 17, height: 19) }
      end
    ensure
      attached.close
      owner.close
    end
  end
end
