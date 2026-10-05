"""Normalize the specific psutil 7.2.2 macOS procargs error without hiding others."""
import psutil


def cmdline(process):
    try:
        return process.cmdline()
    except SystemError as error:
        # psutil upstream da599756a5fe06fec3693ad66df2cc4d1eeaa83d fixes
        # procargs returning success with PermissionError set. Until a fixed
        # release is pinned, preserve its intended AccessDenied semantics.
        if (str(error) == '<built-in function proc_cmdline> returned a result with an exception set'
                and isinstance(error.__cause__, PermissionError)):
            raise psutil.AccessDenied(process.pid) from error
        raise
