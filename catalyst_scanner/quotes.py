# Load the repository's top-level quotes.py into the package namespace so relative imports work
import importlib.util, importlib, sys, os
src = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'quotes.py'))
name = 'catalyst_scanner.quotes'
spec = importlib.util.spec_from_file_location(name, src)
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
spec.loader.exec_module(module)
for _name in dir(module):
    if not _name.startswith('__'):
        globals()[_name] = getattr(module, _name)
