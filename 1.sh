#!/bin/bash

# Cập nhật hệ thống và cài đặt các gói cần thiết
echo "Cập nhật hệ thống..."
yum -y update
yum -y install epel-release
yum -y install squid httpd-tools python36 python36-pip firewalld curl

# Xác định IPv4 public của VPS
echo "Lấy IPv4 public của VPS..."
IPV4_VPS=$(curl -s ifconfig.me)
echo "IPv4 VPS của bạn là: $IPV4_VPS"

# Cài đặt các thư viện Python cần thiết
echo "Cài đặt các thư viện Python..."
pip3.6 install python-telegram-bot==13.7

# Tải và cấu hình Squid
echo "Cấu hình Squid..."
systemctl stop squid

# Sao lưu file cấu hình Squid
cp /etc/squid/squid.conf /etc/squid/squid.conf.bak

# Tạo file cấu hình Squid cơ bản
cat <<EOF > /etc/squid/squid.conf
acl localnet src 0.0.0.0/0
http_access allow localnet
http_access deny all

auth_param basic program /usr/lib64/squid/basic_ncsa_auth /etc/squid/passwd
auth_param basic children 5
auth_param basic realm Squid Basic Authentication
auth_param basic credentialsttl 2 hours
acl auth_users proxy_auth REQUIRED
http_access allow auth_users

# Listen trên IPv4 VPS (sẽ được script Python thêm từng port riêng)
http_port $IPV4_VPS:3128

# Ưu tiên IPv6 outbound
dns_v4_first off
EOF

# Tạo file lưu trữ mật khẩu
touch /etc/squid/passwd
chown squid:squid /etc/squid/passwd

# Mở tất cả các port (1000-60000)
echo "Mở các port từ 1000 đến 60000..."
systemctl start firewalld
systemctl enable firewalld
firewall-cmd --permanent --add-port=1000-60000/tcp
firewall-cmd --reload

# Khởi động Squid
echo "Khởi động Squid..."
systemctl start squid
systemctl enable squid

# Kiểm tra trạng thái Squid
systemctl status squid

echo "Cài đặt hoàn tất! Bạn có thể chạy proxy.py để tạo và quản lý proxy."