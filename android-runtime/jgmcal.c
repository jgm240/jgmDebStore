/* jgmDeb: voice calibration service (own work).
   Built as a shared library and injected with LD_PRELOAD into a tiny stock Android (bionic) program (tinymix). It hooks mixer_open(), which tinymix calls first
   in main(), loads the stock calibration through the stock libacdbloader: acdb_loader_init_v2(card, cvd, 0), then stays alive (the kernel frees the calibration
   when /dev/msm_audio_cal is closed) and waits on a FIFO for lines "RX TX" (ACDB device ids) to call acdb_loader_send_voice_cal(rx, tx).
   Env: CARD CVD FIFO.  Status lines go to stdout.
   Build (zig, against tiny stub libc.so/libdl.so so the DT_NEEDED entries are right):
     zig cc -target arm-linux-gnueabi -mcpu=cortex_a7 -shared -fPIC -nostdlib -fno-stack-protector -fno-builtin -fno-unwind-tables \
            -fno-asynchronous-unwind-tables -O1 -Wl,--strip-all -o jgmcal.so jgmcal.c stub/libc.so stub/libdl.so */
extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);
extern char *dlerror(void);
extern int printf(const char *, ...);
extern char *getenv(const char *);
extern int atoi(const char *);
extern void _exit(int);
extern int fflush(void *);
extern long write(int, const void *, unsigned long);
extern long read(int, void *, unsigned long);
extern int open(const char *, int, ...);
static void run(void) {
  const char *card = getenv("CARD"); if (!card) card = "msm8909-skub-snd-card";
  const char *cvd = getenv("CVD"); if (!cvd) cvd = "2.3";
  const char *fifo = getenv("FIFO"); if (!fifo) fifo = "/run/jgmcal.fifo";
  void *h = dlopen("libacdbloader.so", 2);
  if (!h) { printf("dlopen failed: %s\n", dlerror()); fflush(0); _exit(2); }
  int (*init)(const char *, const char *, int) = (int (*)(const char *, const char *, int))dlsym(h, "acdb_loader_init_v2");
  int (*send)(int, int) = (int (*)(int, int))dlsym(h, "acdb_loader_send_voice_cal");
  if (!init || !send) { printf("dlsym failed: %s\n", dlerror()); fflush(0); _exit(3); }
  int r = init(card, cvd, 0); printf("init %d\n", r); fflush(0);
  if (r != 0) _exit(4);
  int fd = open(fifo, 2 /* O_RDWR: never sees EOF */);
  if (fd < 0) { printf("cannot open %s\n", fifo); fflush(0); _exit(5); }
  printf("ready\n"); fflush(0);
  char buf[64]; int n = 0;
  for (;;) {                                   /* lines "RX TX\n" */
    char c; if (read(fd, &c, 1) != 1) continue;
    if (c != '\n') { if (n < 62) buf[n++] = c; continue; }
    buf[n] = 0; n = 0; int i = 0, rx = atoi(buf); while (buf[i] && buf[i] != ' ') i++; int tx = atoi(buf + i);
    r = send(rx, tx); printf("sent rx=%d tx=%d -> %d\n", rx, tx, r); fflush(0);
  }
}
void *mixer_open(unsigned int card) { run(); return 0; }
