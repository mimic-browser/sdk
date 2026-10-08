require 'minitest/autorun'
require 'mimic_sdk/ferrum'

class FerrumIntegrationTest < Minitest::Test
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
