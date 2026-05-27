#!/usr/bin/env python3
"""
disclaim-exec.py — posix_spawn the target binary with
POSIX_SPAWN_SETDISCLAIM set via responsibility_spawnattrs_setdisclaim().

The spawned target becomes its OWN responsible process — TCC stops
attributing its data accesses back to this python interpreter.

Used by claude-ephemeral.sh so claude.exe runs disclaimed from the
voice-wrapper python3.13 chain, eliminating recurring "python3.13 would
like to access data from other apps" popups against com.anthropic.claude-code.

Background: ~/Context/areas/setup/phone-access.md decisions log, entry
dated 2026-05-26 (initial TCC.db Allow-row theory, since corrected) and
the followup that documents this wrapper as the actual fix.

Usage: disclaim-exec.py <target> [args...]
"""
import ctypes
import ctypes.util
import os
import signal
import sys


def _die(msg, code=1):
    print(f"disclaim-exec: {msg}", file=sys.stderr)
    sys.exit(code)


if len(sys.argv) < 2:
    _die("usage: disclaim-exec.py <target> [args...]", 2)

libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

posix_spawnattr_t = ctypes.c_void_p
pid_t = ctypes.c_int

libc.posix_spawnattr_init.argtypes = [ctypes.POINTER(posix_spawnattr_t)]
libc.posix_spawnattr_init.restype = ctypes.c_int
libc.posix_spawnattr_destroy.argtypes = [ctypes.POINTER(posix_spawnattr_t)]
libc.posix_spawnattr_destroy.restype = ctypes.c_int

# Private libsystem API; stable since macOS 10.14.
# int responsibility_spawnattrs_setdisclaim(posix_spawnattr_t *attr, int disclaim);
libc.responsibility_spawnattrs_setdisclaim.argtypes = [
    ctypes.POINTER(posix_spawnattr_t), ctypes.c_int
]
libc.responsibility_spawnattrs_setdisclaim.restype = ctypes.c_int

libc.posix_spawnp.argtypes = [
    ctypes.POINTER(pid_t),
    ctypes.c_char_p,
    ctypes.c_void_p,
    ctypes.POINTER(posix_spawnattr_t),
    ctypes.POINTER(ctypes.c_char_p),
    ctypes.POINTER(ctypes.c_char_p),
]
libc.posix_spawnp.restype = ctypes.c_int

attr = posix_spawnattr_t()
if libc.posix_spawnattr_init(ctypes.byref(attr)) != 0:
    _die(f"posix_spawnattr_init failed: errno={ctypes.get_errno()}")

rc = libc.responsibility_spawnattrs_setdisclaim(ctypes.byref(attr), 1)
if rc != 0:
    libc.posix_spawnattr_destroy(ctypes.byref(attr))
    _die(f"responsibility_spawnattrs_setdisclaim failed: rc={rc}")

target = sys.argv[1].encode()
argv_items = [a.encode() for a in sys.argv[1:]] + [None]
ArgvT = ctypes.c_char_p * len(argv_items)
argv_arr = ArgvT(*argv_items)

env_items = [f"{k}={v}".encode() for k, v in os.environ.items()] + [None]
EnvT = ctypes.c_char_p * len(env_items)
envp_arr = EnvT(*env_items)

pid = pid_t(0)
rc = libc.posix_spawnp(
    ctypes.byref(pid), target,
    None, ctypes.byref(attr),
    argv_arr, envp_arr,
)
libc.posix_spawnattr_destroy(ctypes.byref(attr))

if rc != 0:
    _die(f"posix_spawnp({sys.argv[1]}) failed: {os.strerror(rc)}")

child_pid = pid.value


def _forward(signum, _frame):
    try:
        os.kill(child_pid, signum)
    except ProcessLookupError:
        pass


for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT,
            signal.SIGWINCH, signal.SIGUSR1, signal.SIGUSR2):
    try:
        signal.signal(sig, _forward)
    except (OSError, ValueError):
        pass

while True:
    try:
        _, status = os.waitpid(child_pid, 0)
        break
    except InterruptedError:
        continue

if os.WIFEXITED(status):
    sys.exit(os.WEXITSTATUS(status))
elif os.WIFSIGNALED(status):
    sys.exit(128 + os.WTERMSIG(status))
sys.exit(1)
