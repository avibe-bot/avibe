#!/usr/bin/env bash
set -euo pipefail

out=${1:?output directory}
mkdir -p "$out"
rm -f "$out"/ca.key.pem "$out"/ca.cert.pem "$out"/server.csr.pem "$out"/key.pem "$out"/cert.pem "$out"/Anchor.der
cat >"$out/openssl.cnf" <<'EOF'
[ req ]
distinguished_name = req_dn
prompt = no
[ req_dn ]
CN = avibe-webkit-proxy-change-test-ca
[ ca_ext ]
basicConstraints = critical, CA:true, pathlen:1
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
[ server_ext ]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:*.webkit-proxy.test, DNS:webkit-proxy.test
EOF
openssl genrsa -out "$out/ca.key.pem" 2048 >/dev/null 2>&1
openssl req -x509 -new -sha256 -key "$out/ca.key.pem" -out "$out/ca.cert.pem" \
  -days 2 -config "$out/openssl.cnf" -extensions ca_ext >/dev/null 2>&1
openssl genrsa -out "$out/key.pem" 2048 >/dev/null 2>&1
openssl req -new -sha256 -key "$out/key.pem" -out "$out/server.csr.pem" \
  -subj "/CN=webkit-proxy.test" >/dev/null 2>&1
openssl x509 -req -sha256 -in "$out/server.csr.pem" -CA "$out/ca.cert.pem" \
  -CAkey "$out/ca.key.pem" -CAcreateserial -out "$out/cert.pem" -days 2 \
  -extfile "$out/openssl.cnf" -extensions server_ext >/dev/null 2>&1
openssl x509 -in "$out/ca.cert.pem" -outform DER -out "$out/Anchor.der"
chmod 600 "$out"/ca.key.pem "$out"/key.pem
openssl verify -CAfile "$out/ca.cert.pem" "$out/cert.pem"
