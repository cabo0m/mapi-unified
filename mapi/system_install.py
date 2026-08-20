from __future__ import annotations

import sys

from mapi_platform.linux import system_install as _impl

sys.modules[__name__] = _impl
