# -*- coding: utf-8 -*-
"""生成局域网自签名 HTTPS 证书（PWA 在手机上安装需要安全上下文，ADR-009）。

用法（在项目根目录）：
    .\\.venv\\Scripts\\python.exe tools/make_cert.py            # 自动探测本机局域网 IP
    .\\.venv\\Scripts\\python.exe tools/make_cert.py --ip 192.168.1.5

产物：out/cert.pem + out/key.pem（已被 .gitignore 排除）。
IP 变了就重新跑一次。浏览器首次访问 https://<IP>:8765 会出现证书警告，
属自签名证书的正常现象，点「高级 → 继续前往」即可。
"""
import argparse
import datetime
import ipaddress
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def detect_ip():
    """探测本机局域网 IP（不真正发包，只走一次 UDP connect 决定路由）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default=detect_ip(), help="局域网 IP（默认自动探测）")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "out"))
    a = ap.parse_args()
    ipaddress.ip_address(a.ip)  # 校验格式

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PicNamer Local")])
    san = x509.SubjectAlternativeName([
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address(a.ip)),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(san, critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (out / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    print(f"cert ok for ip: {a.ip}")


if __name__ == "__main__":
    main()
