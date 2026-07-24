# Load top-level app.py as catalyst_scanner.app for tests
import importlib.util, sys, os
src = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'app.py'))
name = 'catalyst_scanner.app'
spec = importlib.util.spec_from_file_location(name, src)
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
spec.loader.exec_module(module)
for _name in dir(module):
    if not _name.startswith('__'):
        globals()[_name] = getattr(module, _name)
