# PyInstaller spec for freezing the Deja Python backend
# into a single self-contained binary. No Python installation needed.
#
# Build: ./venv/bin/pyinstaller deja.spec
# Output: dist/deja-backend (50MB arm64 binary)

import os
from PyInstaller.utils.hooks import collect_submodules

# Collect all deja submodules automatically
hiddenimports = collect_submodules('deja')
hiddenimports += [
    'uvicorn',
    'uvicorn.logging',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.http.h11_impl',
    'uvicorn.protocols.http.httptools_impl',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    'uvicorn.lifespan.off',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.loops.asyncio',
    'fastapi',
    'starlette',
    'starlette.responses',
    'starlette.middleware',
    'starlette.middleware.cors',
    'httptools',
    'mcp',
    'google.genai',
]

a = Analysis(
    ['src/deja/__main__.py'],
    pathex=['src'],
    hiddenimports=hiddenimports,
    excludes=['tkinter', 'test', 'unittest'],
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    name='deja-backend',
    strip=True,
    upx=False,
    target_arch='arm64',
    exclude_binaries=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name='deja-backend',
    strip=True,
)
