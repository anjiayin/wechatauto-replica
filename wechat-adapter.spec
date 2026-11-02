# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('wx_adapter_config.json', '.')]
binaries = []
hiddenimports = ['wechatauto', 'wechatauto.db', 'wechatauto.guia', 'wechatauto.sender', 'wechatauto.media', 'wechatauto.moment', 'wechatauto.uia_driver', 'wechatauto.utils', 'wechatauto.utils.lock', 'wechatauto.ui', 'wechatauto.uia', 'wechatauto.msgs', 'websockets', 'websockets.legacy', 'websockets.legacy.server', 'uiautomation', 'win32api', 'win32gui', 'win32con', 'win32process', 'pywintypes', 'pyperclip', 'PIL', 'psutil', 'colorama', 'cryptography', 'winsdk', 'imageio_ffmpeg', 'zstandard', 'pyautogui', 'cv2', 'numpy']
tmp_ret = collect_all('wechatauto')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('websockets')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('uiautomation')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['adapter/__main__.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='wechat-adapter',
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
