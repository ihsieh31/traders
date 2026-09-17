"""Inherited offline guard for the refactor runner, including Python children.

Blocks all INET traffic (including localhost), DNS and non-Python child programs.
An audit hook survives tests monkeypatching socket methods. This is a test guard,
not an OS security boundary against native extensions or deliberately hostile code.
"""
import os
import sys

if os.environ.get('REFACTOR_OFFLINE') == '1':
    import json
    from pathlib import Path
    import socket

    _artifacts = Path(os.environ['REFACTOR_ARTIFACTS'])
    _guard_dir = str(Path(__file__).resolve().parent)

    def _deny(event, detail):
        with (_artifacts / 'guard-events.jsonl').open('a') as stream:
            stream.write(json.dumps({'pid': os.getpid(), 'event': event, 'detail': str(detail)}) + '\n')
        raise RuntimeError('Offline refactor guard blocked ' + event + ': ' + str(detail))

    def _audit(event, args):
        if event in ('socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr'):
            _deny(event, args)
        if event in ('socket.connect', 'socket.sendto', 'socket.bind'):
            if args[0].family != socket.AF_UNIX:
                _deny(event, args[1:])
        if event in ('os.system', 'os.exec', 'os.posix_spawn'):
            _deny(event, args[0])
        if event == 'subprocess.Popen':
            executable, argv, cwd, child_env = args
            name = Path(os.fsdecode(executable)).name
            if not name.startswith('python'):
                _deny(event, executable)
            if not isinstance(argv, (list, tuple)) or any(x in ('-I', '-E', '-S') for x in argv[1:]):
                _deny(event, 'Python child disables inherited guard')
            effective_env = os.environ if child_env is None else child_env
            if (effective_env.get('REFACTOR_OFFLINE') != '1'
                    or effective_env.get('PYTHON_DOTENV_DISABLED') != '1'
                    or _guard_dir not in effective_env.get('PYTHONPATH', '').split(os.pathsep)):
                _deny(event, 'Python child strips offline environment')
            if '-m' in argv:
                index = argv.index('-m')
                if argv[index + 1:index + 2] == ['pip'] and 'install' in argv[index + 2:]:
                    _deny(event, 'dependency installation forbidden')

    sys.addaudithook(_audit)
    os.environ['REFACTOR_GUARD_ACTIVE'] = '1'
