#!/bin/bash

# Cài đặt screen
yum install screen -y

# Tải file 1.sh
wget -O 1.sh https://raw.githubusercontent.com/pzkdli/New/refs/heads/main/1.sh

# Cấp quyền chạy
chmod +x 1.sh

# Chạy file 1.sh
bash 1.sh

# Tải file 2.py
wget -O 2.py https://raw.githubusercontent.com/pzkdli/New/refs/heads/main/2.py

# Cài đặt Python nếu cần
yum install python36 -y

# Chạy 2.py trong screen (tạo screen mới tên 'tool')
screen -dmS tool python3.6 2.py
