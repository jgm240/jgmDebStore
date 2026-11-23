# Stock Android runtime for voice calibration (local use only)
The modem's voice DSP needs the stock audio calibration (ACDB) loaded into the kernel before every call. Android does this with closed libraries, so jgmDeb runs those
same stock libraries: `/system` on the phone holds the Android linker, 17 stock libraries, a tiny host program (tinymix) and the stock `acdbdata` files, all taken from
the phone's own stock system image. **These are vendor files: never publish them.** `jgmcal.c` (own code) is injected into the host program with LD_PRELOAD.
Needed on the phone: `/vendor -> /system/vendor`, `/etc/acdbdata -> /system/etc/acdbdata`. Control: `jgmcal-daemon.sh start|send RX TX`; call-audio.sh does it per call.
Local archive of the runtime: ~/jgmgo-debian/androidroot.tar (extract at / on the phone).
