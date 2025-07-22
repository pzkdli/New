import random
import string
import subprocess
import datetime
import os
import threading
import time
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext
import sqlite3
import ipaddress
import json

# Thiết lập logging để debug lỗi
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Thay thế bằng địa chỉ IPv4 tĩnh của VPS của bạn
# Địa chỉ IPv4 này sẽ được sử dụng cho các kết nối client đến proxy.
VPS_IPV4 = "103.252.137.149" # <--- QUAN TRỌNG: Thay thế bằng địa chỉ IPv4 thực tế của VPS của bạn

# Kết nối cơ sở dữ liệu SQLite
def init_db():
    conn = sqlite3.connect('proxies.db')
    c = conn.cursor()
    # Đã loại bỏ cột 'ipv4' vì nó sẽ là IPv4 của VPS chung, không phải của từng proxy riêng lẻ.
    c.execute('''CREATE TABLE IF NOT EXISTS proxies
                 (ipv6 TEXT, port INTEGER, user TEXT, password TEXT, expiry_date TEXT, is_used INTEGER)''')
    conn.commit()
    conn.close()

# Tạo user ngẫu nhiên (vtoanXXXY)
def generate_user():
    numbers = ''.join(random.choices(string.digits, k=3))
    letter = random.choice(string.ascii_uppercase)
    return f"vtoan{numbers}{letter}"

# Tạo mật khẩu ngẫu nhiên (2 chữ cái in hoa)
def generate_password():
    return ''.join(random.choices(string.ascii_uppercase, k=2))

# Kiểm tra định dạng prefix IPv6
def validate_ipv6_prefix(prefix):
    try:
        ipaddress.IPv6Network(prefix, strict=False)
        return True
    except ValueError:
        logger.error(f"Prefix IPv6 không hợp lệ: {prefix}")
        return False

# Kiểm tra IPv6 có hoạt động trên VPS
def check_ipv6_support():
    try:
        result = subprocess.run(['ping6', '-c', '1', 'ipv6.google.com'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=5)
        if result.returncode == 0:
            logger.info("IPv6 hoạt động trên VPS")
            return True
        else:
            logger.error(f"IPv6 không hoạt động: {result.stderr}")
            return False
    except Exception as e:
        logger.error(f"Lỗi khi kiểm tra IPv6: {e}")
        return False

# Tạo địa chỉ IPv6 ngẫu nhiên từ prefix
def generate_ipv6_from_prefix(prefix, num_addresses):
    try:
        network = ipaddress.IPv6Network(prefix, strict=False)
        base_addr = int(network.network_address)
        max_addr = int(network.broadcast_address)
        ipv6_addresses = []
        
        conn = sqlite3.connect('proxies.db')
        c = conn.cursor()
        c.execute("SELECT ipv6 FROM proxies")
        used_ipv6 = [row[0] for row in c.fetchall()]
        conn.close()
        
        for _ in range(num_addresses):
            while True:
                random_addr = base_addr + random.randint(0, max_addr - base_addr)
                ipv6 = str(ipaddress.IPv6Address(random_addr))
                if ipv6 not in used_ipv6:
                    ipv6_addresses.append(ipv6)
                    used_ipv6.append(ipv6)
                    break
        
        return ipv6_addresses
    except Exception as e:
        logger.error(f"Lỗi khi tạo IPv6 từ prefix {prefix}: {e}")
        raise

# Kiểm tra kết nối proxy thực tế
# Sử dụng VPS_IPV4 để kết nối đến proxy, nhưng kiểm tra xem proxy có trả về IPv6 hay không.
def check_proxy_usage(ipv4_vps, port, user, password, expected_ipv6):
    try:
        # Cố gắng sử dụng curl -6 để ưu tiên IPv6, nếu không thì dùng curl bình thường
        cmd = f'curl --proxy http://{user}:{password}@{ipv4_vps}:{port} --connect-timeout 5 https://api64.ipify.org?format=json'
        result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
        
        if result.returncode == 0:
            response = json.loads(result.stdout)
            ip = response.get('ip', '')
            try:
                ipaddress.IPv6Address(ip)
                logger.info(f"Proxy {ipv4_vps}:{port} trả về IPv6: {ip}")
                if ip != expected_ipv6:
                    logger.warning(f"Proxy {ipv4_vps}:{port} trả về IPv6 {ip} không khớp với {expected_ipv6}")
                return True, ip
            except ValueError:
                # Nếu không phải IPv6, nó là IPv4.
                logger.warning(f"Proxy {ipv4_vps}:{port} trả về IPv4: {ip} thay vì IPv6. Cần kiểm tra lại cấu hình Squid.")
                return False, ip # Trả về False để đánh dấu là không như mong muốn (chỉ IPv6)
        else:
            logger.error(f"Proxy {ipv4_vps}:{port} không kết nối được: {result.stderr}")
            return False, None
    except Exception as e:
        logger.error(f"Lỗi khi kiểm tra proxy {ipv4_vps}:{port}: {e}")
        return False, None

# Tự động kiểm tra proxy mỗi 60 giây
def auto_check_proxies():
    while True:
        try:
            conn = sqlite3.connect('proxies.db')
            c = conn.cursor()
            # Lấy thông tin proxy, sử dụng VPS_IPV4 làm IPv4 kết nối
            c.execute("SELECT ipv6, port, user, password FROM proxies")
            proxies_data = c.fetchall()
            
            for proxy_info in proxies_data:
                ipv6, port, user, password = proxy_info
                # Pass VPS_IPV4 cho hàm kiểm tra
                is_used, returned_ip = check_proxy_usage(VPS_IPV4, port, user, password, ipv6)
                # Cập nhật trạng thái sử dụng dựa trên ipv6, port, user, password
                c.execute("UPDATE proxies SET is_used=? WHERE ipv6=? AND port=? AND user=? AND password=?",
                          (1 if is_used else 0, ipv6, port, user, password))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Lỗi khi kiểm tra proxy tự động: {e}")
        time.sleep(60)

# Tạo proxy mới với danh sách IPv6
def create_proxy(ipv4_vps, ipv6_addresses, days):
    try:
        if not check_ipv6_support():
            raise Exception("IPv6 không hoạt động trên VPS. Vui lòng kiểm tra cấu hình mạng.")
        
        conn = sqlite3.connect('proxies.db')
        c = conn.cursor()
        
        c.execute("SELECT port FROM proxies")
        used_ports = [row[0] for row in c.fetchall()]
        
        proxies_output = [] # Danh sách các proxy để hiển thị ra người dùng
        # Đảm bảo file squid.conf có cấu hình cơ bản và ghi đè
        squid_conf_base = """
acl SSL_ports port 443
acl Safe_ports port 80
acl Safe_ports port 443
acl CONNECT method CONNECT
http_access deny !Safe_ports
http_access deny CONNECT !SSL_ports
acl localnet src all
http_access allow localnet
http_access deny all
auth_param basic program /usr/lib64/squid/basic_ncsa_auth /etc/squid/passwd
auth_param basic children 5
auth_param basic realm Squid Basic Authentication
auth_param basic credentialsttl 2 hours
acl auth_users proxy_auth REQUIRED
http_access allow auth_users
http_port 3128  # <--- THÊM DÒNG NÀY ĐỂ SQUID KHỞI ĐỘNG
"""
        with open('/etc/squid/squid.conf', 'w') as f:
            f.write(squid_conf_base)
        
        for ipv6 in ipv6_addresses:
            # Gán IPv6 vào giao diện
            result = subprocess.run(['ip', '-6', 'addr', 'add', f'{ipv6}/64', 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            if result.returncode != 0:
                logger.error(f"Lỗi khi gán IPv6 {ipv6}: {result.stderr}")
                raise Exception(f"Lỗi khi gán IPv6 {ipv6}: {result.stderr}")
            
            while True:
                port = random.randint(1000, 60000)
                if port not in used_ports:
                    used_ports.append(port)
                    break
            
            user = generate_user()
            password = generate_password()
            expiry_date = (datetime.datetime.now() + datetime.timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
            
            # Lưu thông tin proxy vào cơ sở dữ liệu
            c.execute("INSERT INTO proxies (ipv6, port, user, password, expiry_date, is_used) VALUES (?, ?, ?, ?, ?, ?)",
                      (ipv6, port, user, password, expiry_date, 0))
            
            # Thêm cấu hình Squid cho mỗi proxy
            with open('/etc/squid/squid.conf', 'a') as f:
                f.write(f"acl proxy_{user} myport {port}\n")
                f.write(f"tcp_outgoing_address {ipv6} proxy_{user}\n")
                # ĐÂY LÀ ĐIỂM CHÍNH: Ràng buộc http_port với IPv4 của VPS
                f.write(f"http_port {ipv4_vps}:{port}\n") 
            
            # Thêm user vào file passwd
            result = subprocess.run(['htpasswd', '-b', '/etc/squid/passwd', user, password], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            if result.returncode != 0:
                logger.error(f"Lỗi khi thêm user {user} vào /etc/squid/passwd: {result.stderr}")
                raise Exception(f"Lỗi khi thêm user {user}: {result.stderr}")
            
            # Định dạng đầu ra theo yêu cầu: ipv4_của_vps:port:user:pass
            proxies_output.append(f"{ipv4_vps}:{port}:{user}:{password}")
        
        conn.commit()
        conn.close()
        
        # Kiểm tra cấu hình Squid
        result = subprocess.run(['squid', '-k', 'check'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if result.returncode != 0:
            logger.error(f"Lỗi cấu hình Squid: {result.stderr}")
            raise Exception(f"Lỗi cấu hình Squid: {result.stderr}")
        
        # Restart Squid
        result = subprocess.run(['systemctl', 'restart', 'squid'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if result.returncode != 0:
            logger.error(f"Lỗi khi restart Squid: {result.stderr}")
            raise Exception(f"Lỗi khi restart Squid: {result.stderr}")
        
        logger.info(f"Đã tạo {len(proxies_output)} proxy với IPv6")
        
        return proxies_output
    except Exception as e:
        logger.error(f"Lỗi khi tạo proxy: {e}")
        raise

# Telegram bot commands
def start(update: Update, context: CallbackContext):
    if update.message.from_user.id != 7550813603: # <--- QUAN TRỌNG: Thay đổi ID người dùng này nếu cần quyền truy cập bot
        update.message.reply_text("Bạn không có quyền sử dụng bot này!")
        return
    
    update.message.reply_text("Nhập prefix IPv6 (định dạng: 2401:2420:0:102f::/64 hoặc 2401:2420:0:102f:0000:0000:0000:0001/64):")
    context.user_data['state'] = 'prefix'

def button(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
    if query.data == 'new':
        if 'prefix' not in context.user_data: # Không cần kiểm tra 'ipv4' ở đây nữa
            query.message.reply_text("Vui lòng nhập prefix IPv6 trước bằng lệnh /start!")
            return
        query.message.reply_text("Nhập số lượng proxy và số ngày (định dạng: số_lượng số_ngày, ví dụ: 5 7):")
        context.user_data['state'] = 'new'
    elif query.data == 'xoa':
        keyboard = [
            [InlineKeyboardButton("Xóa proxy lẻ", callback_data='xoa_le'),
             InlineKeyboardButton("Xóa hàng loạt", callback_data='xoa_all')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        query.message.reply_text("Chọn kiểu xóa:", reply_markup=reply_markup)
    elif query.data == 'check':
        conn = sqlite3.connect('proxies.db')
        c = conn.cursor()
        # Lấy thông tin ipv6 để sử dụng cho output, không lấy ipv4 riêng lẻ
        c.execute("SELECT ipv6, port, user, password, is_used FROM proxies")
        proxies = c.fetchall()
        conn.close()
        
        waiting = [p for p in proxies if p[4] == 0]
        used = [p for p in proxies if p[4] == 1]
        
        # Ghi file với định dạng output: ipv4_vps:port:user:pass
        with open('waiting.txt', 'w') as f:
            for p in waiting:
                f.write(f"{VPS_IPV4}:{p[1]}:{p[2]}:{p[3]}\n")
        with open('used.txt', 'w') as f:
            for p in used:
                f.write(f"{VPS_IPV4}:{p[1]}:{p[2]}:{p[3]}\n")
        
        try:
            context.bot.send_document(chat_id=update.effective_chat.id, document=open('waiting.txt', 'rb'), caption="Danh sách proxy chờ")
            context.bot.send_document(chat_id=update.effective_chat.id, document=open('used.txt', 'rb'), caption="Danh sách proxy đã sử dụng")
            query.message.reply_text(f"Proxy chờ: {len(waiting)}\nProxy đã sử dụng: {len(used)}\nFile waiting.txt và used.txt đã được gửi.")
        except Exception as e:
            logger.error(f"Lỗi khi gửi file waiting.txt/used.txt: {e}")
            query.message.reply_text(f"Proxy chờ: {len(waiting)}\nProxy đã sử dụng: {len(used)}\nLỗi khi gửi file: {e}")
    elif query.data == 'giahan':
        query.message.reply_text("Nhập proxy và số ngày gia hạn (định dạng: ipv4_vps:port:user:pass số_ngày):")
        context.user_data['state'] = 'giahan'
    elif query.data == 'xoa_le':
        query.message.reply_text("Nhập proxy cần xóa (định dạng: ipv4_vps:port:user:pass):")
        context.user_data['state'] = 'xoa_le'
    elif query.data == 'xoa_all':
        query.message.reply_text("Xác nhận xóa tất cả proxy? (Nhập: Xac_nhan_xoa_all)")
        context.user_data['state'] = 'xoa_all'

def message_handler(update: Update, context: CallbackContext):
    if update.message.from_user.id != 7550813603: # <--- QUAN TRỌNG: Thay đổi ID người dùng này nếu cần quyền truy cập bot
        update.message.reply_text("Bạn không có quyền sử dụng bot này!")
        return
    
    state = context.user_data.get('state')
    text = update.message.text.strip()
    
    if state == 'prefix':
        if validate_ipv6_prefix(text):
            context.user_data['prefix'] = text
            # Không cần hỏi IPv4 nữa, dùng VPS_IPV4 toàn cục
            keyboard = [
                [InlineKeyboardButton("/New", callback_data='new'),
                 InlineKeyboardButton("/Xoa", callback_data='xoa')],
                [InlineKeyboardButton("/Check", callback_data='check'),
                 InlineKeyboardButton("/Giahan", callback_data='giahan')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            update.message.reply_text(f"Prefix IPv6 đã được lưu. IPv4 của VPS: {VPS_IPV4}. Chọn lệnh:", reply_markup=reply_markup)
            context.user_data['state'] = None
        else:
            update.message.reply_text("Prefix IPv6 không hợp lệ! Vui lòng nhập lại:")
    elif state == 'new':
        try:
            num_proxies, days = map(int, text.split())
            if num_proxies <= 0 or days <= 0:
                update.message.reply_text("Số lượng và số ngày phải lớn hơn 0!")
                return
            prefix = context.user_data.get('prefix')
            if not prefix:
                update.message.reply_text("Vui lòng nhập prefix IPv6 trước bằng lệnh /start!")
                return
            
            ipv6_addresses = generate_ipv6_from_prefix(prefix, num_proxies)
            # Truyền VPS_IPV4 cho hàm tạo proxy
            proxies = create_proxy(VPS_IPV4, ipv6_addresses, days) 
            
            if num_proxies < 5:
                update.message.reply_text("Proxy đã tạo:\n" + "\n".join(proxies))
            else:
                with open('proxies.txt', 'w') as f:
                    for proxy in proxies:
                        f.write(f"{proxy}\n")
                try:
                    context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=open('proxies.txt', 'rb'),
                        caption=f"Đã tạo {num_proxies} proxy",
                        timeout=30
                    )
                except Exception as e:
                    logger.error(f"Lỗi khi gửi file proxies.txt: {e}")
                    update.message.reply_text(f"Đã tạo {num_proxies} proxy nhưng lỗi khi gửi file: {e}\nFile proxies.txt đã được lưu trên hệ thống.")
            
            context.user_data['state'] = None
        except Exception as e:
            logger.error(f"Lỗi khi xử lý lệnh /New: {e}")
            update.message.reply_text(f"Định dạng không hợp lệ hoặc lỗi: {e}")
    elif state == 'giahan':
        try:
            proxy_str, days_str = text.rsplit(' ', 1)
            # Parse proxy string để lấy port, user, pass
            # IPv4 ở đây sẽ là IPv4 của VPS, không phải của proxy riêng lẻ
            ipv4_from_input, port_str, user, password = proxy_str.split(':')
            port = int(port_str)
            days = int(days_str)

            # Truy vấn database bằng user, port, password để tìm ipv6 tương ứng
            conn = sqlite3.connect('proxies.db')
            c = conn.cursor()
            # Tìm proxy dựa trên port, user, password (đảm bảo duy nhất)
            c.execute("SELECT ipv6, expiry_date FROM proxies WHERE port=? AND user=? AND password=?",
                      (port, user, password))
            result = c.fetchone()
            
            if result:
                ipv6_found, old_expiry_str = result
                old_expiry = datetime.datetime.strptime(old_expiry_str, '%Y-%m-%d %H:%M:%S')
                new_expiry = (old_expiry + datetime.timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
                
                # Cập nhật bằng ipv6 làm khóa
                c.execute("UPDATE proxies SET expiry_date=? WHERE ipv6=?",
                          (new_expiry, ipv6_found))
                conn.commit()
                update.message.reply_text(f"Đã gia hạn proxy {proxy_str} thêm {days} ngày. IPv6 gốc: {ipv6_found}")
            else:
                update.message.reply_text("Proxy không tồn tại! Vui lòng kiểm tra lại IPv4 của VPS, Port, User, Pass.")
            conn.close()
            context.user_data['state'] = None
        except Exception as e:
            logger.error(f"Lỗi khi gia hạn proxy: {e}")
            update.message.reply_text(f"Định dạng không hợp lệ hoặc lỗi: {e}\nVui lòng nhập: ipv4_vps:port:user:pass số_ngày")

    elif state == 'xoa_le':
        try:
            proxy_str = text
            # Parse proxy string để lấy port, user, pass
            ipv4_from_input, port_str, user, password = proxy_str.split(':')
            port = int(port_str)
            
            conn = sqlite3.connect('proxies.db')
            c = conn.cursor()
            # Tìm proxy dựa trên port, user, password để lấy IPv6
            c.execute("SELECT ipv6 FROM proxies WHERE port=? AND user=? AND password=?",
                      (port, user, password))
            result = c.fetchone()
            
            if result:
                ipv6_to_delete = result[0]
                c.execute("DELETE FROM proxies WHERE ipv6=?", (ipv6_to_delete,))
                conn.commit()
                
                # Xóa IPv6 khỏi giao diện
                subprocess.run(['ip', '-6', 'addr', 'del', f'{ipv6_to_delete}/64', 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                
                # Xóa user khỏi htpasswd
                subprocess.run(['htpasswd', '-D', '/etc/squid/passwd', user], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                
                # Cập nhật file squid.conf bằng cách loại bỏ các dòng cấu hình của proxy đã xóa
                with open('/etc/squid/squid.conf', 'r') as f:
                    lines = f.readlines()
                with open('/etc/squid/squid.conf', 'w') as f:
                    for line in lines:
                        if f"acl proxy_{user}" not in line and f"tcp_outgoing_address {ipv6_to_delete}" not in line and f"http_port {ipv4_from_input}:{port}" not in line:
                            f.write(line)
                
                # Restart Squid để áp dụng thay đổi
                subprocess.run(['system
