Gem::Specification.new do |spec|
  spec.name = 'mimic-browser'
  spec.version = '0.1.1'
  spec.summary = 'Pinned Mimic runtimes and typed capabilities beside native browser clients'
  spec.authors = ['Mimic Browser contributors']
  spec.homepage = 'https://github.com/mimic-browser/sdk'
  spec.license = 'Prosperity-3.0.0'
  spec.required_ruby_version = '>= 3.2'
  spec.files = Dir['lib/**/*', 'bin/*', 'README.md', 'LICENSE']
  spec.require_paths = ['lib']
  spec.bindir = 'bin'
  spec.executables = ['mimic-sdk']
  spec.add_dependency 'websocket-driver', '~> 0.8'
  spec.add_dependency 'rubyzip', '>= 2.4', '< 4'
end
