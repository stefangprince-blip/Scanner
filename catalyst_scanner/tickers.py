# Load the repository's top-level tickers.py into the package namespace so relative imports work
import importlib.util, importlib, sys, os
src = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'tickers.py'))
name = 'catalyst_scanner.tickers'
spec = importlib.util.spec_from_file_location(name, src)
module = importlib.util.module_from_spec(spec)
# ensure package-level import semantics
sys.modules[name] = module
spec.loader.exec_module(module)
# Export module attributes
for _name in dir(module):
    if not _name.startswith('__'):
        globals()[_name] = getattr(module, _name)
