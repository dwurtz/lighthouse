# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['src/deja/__main__.py'],
    pathex=['src'],
    binaries=[],
    datas=[],
    hiddenimports=['deja.observations.imessage', 'deja.observations.whatsapp', 'deja.observations.clipboard', 'deja.observations.screenshot', 'deja.observations.browser', 'deja.observations.email', 'deja.observations.calendar', 'deja.observations.drive', 'deja.observations.tasks', 'deja.observations.meet', 'deja.observations.contacts', 'deja.meeting_transcribe', 'deja.meeting_coordinator', 'deja.goal_actions', 'deja.goals', 'deja.reflection', 'deja.mcp_server', 'deja.web', 'deja.agent.loop', 'deja.llm.prefilter', 'deja.llm.search', 'uvicorn', 'uvicorn.logging', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan', 'uvicorn.lifespan.on'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='deja-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
