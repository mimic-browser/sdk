require 'minitest/autorun'
require 'mimic_sdk/ferrum'

class FerrumIntegrationTest < Minitest::Test
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
