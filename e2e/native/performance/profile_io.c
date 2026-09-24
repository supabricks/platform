/* Diagnostic-only preload: fixed counters, no file names, no writes of its own. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <time.h>
static pthread_once_t once = PTHREAD_ONCE_INIT;
static int (*real_fsync)(int), (*real_fdatasync)(int);
static _Atomic uint64_t counts[8];
static void initialize(void) {
    real_fsync = dlsym(RTLD_NEXT, "fsync");
    real_fdatasync = dlsym(RTLD_NEXT, "fdatasync");
}
static uint64_t now(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000000ULL + (uint64_t)t.tv_nsec;
}
static int measure(int fd, int kind) {
    pthread_once(&once, initialize);
    int (*call)(int) = kind ? real_fdatasync : real_fsync;
    if (!call) { errno = ENOSYS; return -1; }
    uint64_t start = now(); int result = call(fd); int saved = errno;
    uint64_t elapsed = now() - start; int at = kind * 4;
    atomic_fetch_add(&counts[at], 1); atomic_fetch_add(&counts[at+1], elapsed);
    uint64_t old = atomic_load(&counts[at+2]);
    while (elapsed > old && !atomic_compare_exchange_weak(&counts[at+2], &old, elapsed)) {}
    if (result < 0) atomic_fetch_add(&counts[at+3], 1);
    errno = saved; return result;
}
int fsync(int fd) { return measure(fd, 0); }
int fdatasync(int fd) { return measure(fd, 1); }
void sb_profile_io_snapshot(uint64_t *out) {
    for (int i=0; i<8; i++) out[i] = atomic_load(&counts[i]);
}
