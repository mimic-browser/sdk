require 'minitest/autorun'
require 'yard'

# Parse the distributed source, as YARD-aware editors do, without loading
# Ferrum or launching a browser. Package consumers may override the source root.
class EditorTypesTest < Minitest::Test
  ROOT = ENV.fetch('MIMIC_RUBY_SOURCE', File.expand_path('../lib', __dir__))

  def setup
    if ENV['MIMIC_RUBY_REQUIRE_PACKED'] == '1'
      refute_equal File.realpath(File.expand_path('../lib', __dir__)), File.realpath(ROOT), 'Packed check used source files'
    end
    YARD::Registry.clear
    YARD.parse(Dir[File.join(ROOT, '**/*.rb')])
  end

  def test_native_framework_members_and_factory_context_are_discoverable
    {
      'MimicSDK::Ferrum::Session#browser' => ['::Ferrum::Browser'],
      'MimicSDK::Ferrum::Session#new_context' => ['::Ferrum::Context'],
      'MimicSDK::Ferrum::Session#for_page' => ['MimicSDK::PageClient'],
      'MimicSDK::Ferrum::ContextSetup#browser_context_id' => ['String'],
      'MimicSDK::Ferrum::ContextSetup#mimic' => ['MimicSDK::Client']
    }.each do |name, expected|
      object = YARD::Registry.at(name)
      refute_nil object, "Missing editor-visible #{name}"
      assert_equal expected, object.tag(:return)&.types, name
    end
    %w[launch connect].each do |name|
      factory = YARD::Registry.at("MimicSDK::Ferrum.#{name}")
      assert_equal ['MimicSDK::Ferrum::Session'], factory.tag(:return)&.types
      assert_equal ['MimicSDK::Ferrum::Session'], factory.tags(:yieldparam).find { |tag| tag.name == 'session' }&.types
    end
  end

  def test_generated_commands_expose_typed_collection_members
    object = YARD::Registry.at('MimicSDK::Generated::GetMediaSourcesResult#sources')
    assert_equal ['Array<MediaSource>'], object.tag(:return).types
    object = YARD::Registry.at('MimicSDK::Generated::CreateContextParams#profile')
    assert_equal ['String', 'GenerateProfileSelection'], object.tag(:return).types
    object = YARD::Registry.at('MimicSDK::Generated::MimicCommands#get_media_sources')
    assert_equal ['GetMediaSourcesResult'], object.tag(:return).types
  end
end
