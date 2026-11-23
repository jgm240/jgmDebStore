#!/bin/sh
# jgmDeb: keep apt's big files on /data so the small root partition stays free (own work). Idempotent.
mkdir -p /data/apt/lists/partial /data/apt/archives/partial
if [ ! -L /var/lib/apt/lists ]; then rm -rf /var/lib/apt/lists; ln -s /data/apt/lists /var/lib/apt/lists; fi
if [ ! -L /var/cache/apt/archives ]; then rm -rf /var/cache/apt/archives; ln -s /data/apt/archives /var/cache/apt/archives; fi
cat > /etc/apt/apt.conf.d/98jgm <<'CONF'
Acquire::Languages "none";
APT::Install-Recommends "false";
Dir::Cache::pkgcache "";
Dir::Cache::srcpkgcache "";
CONF
