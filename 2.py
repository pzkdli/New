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

# Lấy địa chỉ IPv6 của VPS
IPV6_VPS = subprocess.getoutput("ip -6 addr show dev eth0 scope global | grep -oP 'inet6 \K[0-9a-f:]+' | head -1")

# Thiết lập logging để debug lỗi
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Kết nối cơ sở dữ liệu SQLite
def init_db():
    conn = sqlite3.connect('proxies.db')
    c = conn.cursor()
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

# Kiểm tra định dạng IPv6
def validate_ipv6(ip):
    try:
        ipaddress.IPv6Address(ip)
        return True
    except ValueError:
        logger.error(f"Địa chỉ IPv6 không hợp lệ: {ip}")
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
def check_proxy_usage(ipv6_vps, port, user, password, expected_ipv6):
    try:
        cmd = f'curl --proxy http://{user}:{password}@[{ipv6_vps}]:{port} --connect-timeout 5 https://api64.ipify.org?format=json'
        result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
        if result.returncode == 0:
            response = json.loads(result.stdout)
            ip = response.get('ip', '')
            try:
                ipaddress.IPv6Address(ip)
                logger.info(f"Proxy [{ipv6_vps}]:{port} trả về IPv6: {ip}")
                if ip != expected_ipv6:
                    logger.warning(f"Proxy [{ipv6_vps}]:{port} trả về IPv6 {ip} không khớp với {expected_ipv6}")
                return True, ip
            except ValueError:
                logger.warning(f"Proxy [{ipv6_vps}]:{port} trả về IPv4: {ip} thay vì IPv6")
                return False, ip
        else:
            logger.error(f"Proxy [{ipv6_vps}]:{port} không kết nối được: {result.stderr}")
            return False, None
    except Exception as e:
        logger.error(f"Lỗi khi kiểm tra proxy [{ipv6_vps}]:{port}: {e}")
        return False, None

# Tự động kiểm tra proxy mỗi 60 giây
def auto_check_proxies():
    while True:
        try:
            conn = sqlite3.connect('proxies.db')
            c = conn.cursor()
            c.execute("SELECT ipv6, port, user, password, ipv6 FROM proxies")
            proxies = c.fetchall()
            
            for proxy in proxies:
                ipv6, port, user, password, expected_ipv6 = proxy
                is_used, returned_ip = check_proxy_usage(IPV6_VPS, port, user, password, expected_ipv6)
                c.execute("UPDATE proxies SET is_used=? WHERE ipv6=? AND port=? AND user=? AND password=?",
                          (1 if is_used else 0, ipv6, port, user, password))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Lỗi khi kiểm tra proxy tự động: {e}")
        time.sleep(60)

# Tạo proxy mới với danh sách IPv6
def create_proxy(ipv6_addresses, days):
    try:
        if not check_ipv6_support():
            raise Exception("IPv6 không hoạt động trên VPS. Vui lòng kiểm tra cấu hình mạng.")
        
        conn = sqlite3.connect('proxies.db')
        c = conn.cursor()
        
        c.execute("SELECT port FROM proxies")
        used_ports = [row[0] for row in c.fetchall()]
        
        proxies = []
        # Đảm bảo file squid.conf có cấu hình cơ bản
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
            
            c.execute("INSERT INTO proxies (ipv6, port, user, password, expiry_date, is_used) VALUES (?, ?, ?, ?, ?, ?)",
                      (ipv6, port, user, password, expiry_date, 0))
            
            # Thêm cấu hình Squid cho mỗi proxy
            with open('/etc/squid/squid.conf', 'a') as f:
                f.write(f"acl proxy_{user} myport {port}\n")
                f.write(f"tcp_outgoing_address {ipv6} proxy_{user}\n")
                f.write(f"http_port [{IPV6_VPS}]:{port}\n")
            
            # Thêm user vào file passwd
            result = subprocess.run(['htpasswd', '-b', '/etc/squid/passwd', user, password], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            if result.returncode != 0:
                logger.error(f"Lỗi khi thêm user {user} vào /etc/squid/passwd: {result.stderr}")
                raise Exception(f"Lỗi khi thêm user {user}: {result.stderr}")
            
            proxies.append(f"[{IPV6_VPS}]:{port}:{user}:{password}")
        
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
        
        logger.info(f"Đã tạo {len(proxies)} proxy với IPv6 tương ứng")
        
        return proxies
    except Exception as e:
        logger.error(f"Lỗi khi tạo proxy: {e}")
        raise

# Telegram bot commands
def start(update: Update, context: CallbackContext):
    if update.message.from_user.id != 7550813603:
        update.message.reply_text("Bạn không có quyền sử dụng bot này!")
        return
    
    update.message.reply_text("Nhập prefix IPv6 (định dạng: 2401:2420:0:102f::/64 hoặc 2401:2420:0:102f:0000:0000:0000:0001/64):")
    context.user_data['state'] = 'prefix'

def button(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
    if query.data == 'new':
        if 'prefix' not in context.user_data:
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
        c.execute("SELECT ipv6, port, user, password, is_used FROM proxies")
        proxies = c.fetchall()
        conn.close()
        
        waiting = [p for p in proxies if p[4] == 0]
        used = [p for p in proxies if p[4] == 1]
        
        with open('waiting.txt', 'w') as f:
            for p in waiting:
                f.write(f"[{IPV6_VPS}]:{p[1]}:{p[2]}:{p[3]}\n")
        with open('used.txt', 'w') as f:
            for p in used:
                f.write(f"[{IPV6_VPS}]:{p[1]}:{p[2]}:{p[3]}\n")
        
        try:
            context.bot.send_document(chat_id=update.effective_chat.id, document=open('waiting.txt', 'rb'), caption="Danh sách proxy chờ")
            context.bot.send_document(chat_id=update.effective_chat.id, document=open('used.txt', 'rb'), caption="Danh sách proxy đã sử dụng")
            query.message.reply_text(f"Proxy chờ: {len(waiting)}\nProxy đã sử dụng: {len(used)}\nFile waiting.txt và used.txt đã được gửi.")
        except Exception as e:
            logger.error(f"Lỗi khi gửi file waiting.txt/used.txt: {e}")
            query.message.reply_text(f"Proxy chờ: {len(waiting)}\nProxy đã sử dụng: {len(used)}\nLỗi khi gửi file: {e}")
    elif query.data == 'giahan':
        query.message.reply_text("Nhập proxy và số ngày gia hạn (định dạng: [IPv6]:port:user:pass số_ngày):")
        context.user_data['state'] = 'giahan'
    elif query.data == 'xoa_le':
        query.message.reply_text("Nhập proxy cần xóa (định dạng: [IPv6]:port:user:pass):")
        context.user_data['state'] = 'xoa_le'
    elif query.data == 'xoa_all':
        query.message.reply_text("Xác nhận xóa tất cả proxy? (Nhập: Xac_nhan_xoa_all)")
        context.user_data['state'] = 'xoa_all'

def message_handler(update: Update, context: CallbackContext):
    if update.message.from_user.id != 7550813603:
        update.message.reply_text("Bạn không có quyền sử dụng bot này!")
        return
    
    state = context.user_data.get('state')
    text = update.message.text.strip()
    
    if state == 'prefix':
        if validate_ipv6_prefix(text):
            context.user_data['prefix'] = text
            keyboard = [
                [InlineKeyboardButton("/New", callback_data='new'),
                 InlineKeyboardButton("/Xoa", callback_data='xoa')],
                [InlineKeyboardButton("/Check", callback_data='check'),
                 InlineKeyboardButton("/Giahan", callback_data='giahan')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            update.message.reply_text("Prefix IPv6 đã được lưu. Chọn lệnh:", reply_markup=reply_markup)
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
            proxies = create_proxy(ipv6_addresses, days)
            
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
            proxy, days = text.rsplit(' ', 1)
            ipv6, port, user, password = proxy.replace('[', '').replace(']', '').split(':')
            days = int(days)
            
            conn = sqlite3.connect('proxies.db')
            c = conn.cursor()
            c.execute("SELECT expiry_date FROM proxies WHERE ipv6=? AND port=? AND user=? AND password=?",
                      (ipv6, int(port), user, password))
            result = c.fetchone()
            if result:
                old_expiry = datetime.datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S')
                new_expiry = (old_expiry + datetime.timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
                c.execute("UPDATE proxies SET expiry_date=? WHERE ipv6=? AND port=? AND user=? AND password=?",
                          (new_expiry, ipv6, int(port), user, password))
                conn.commit()
                update.message.reply_text(f"Đã gia hạn proxy [{ipv6}]:{port}:{user}:{password} thêm {days} ngày.")
            else:
                update.message.reply_text("Proxy không tồn tại!")
            conn.close()
            context.user_data['state'] = None
        except:
            update.message.reply_text("Định dạng không hợp lệ! Vui lòng nhập: [IPv6]:port:user:pass số_ngày")
    elif state == 'xoa_le':
        try:
            ipv6, port, user, password = text.replace('[', '').replace(']', '').split(':')
            conn = sqlite3.connect('proxies.db')
            c = conn.cursor()
            c.execute("SELECT ipv6 FROM proxies WHERE ipv6=? AND port=? AND user=? AND password=?",
                      (ipv6, int(port), user, password))
            result = c.fetchone()
            if result:
                ipv6 = result[0]
                c.execute("DELETE FROM proxies WHERE ipv6=? AND port=? AND user=? AND password=?",
                          (ipv6, int(port), user, password))
                conn.commit()
                
                # Xóa IPv6 khỏi giao diện
                subprocess.run(['ip', '-6', 'addr', 'del', f'{ipv6}/64', 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                
                subprocess.run(['htpasswd', '-D', '/etc/squid/passwd', user], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                
                with open('/etc/squid/squid.conf', 'r') as f:
                    lines = f.readlines()
                with open('/etc/squid/squid.conf', 'w') as f:
                    for line in lines:
                        if f"acl proxy_{user}" not in line and f"tcp_outgoing_address {ipv6}" not in line and f"http_port [{IPV6_VPS}]:{port}" not in line:
                            f.write(line)
                subprocess.run(['systemctl', 'restart', 'squid'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                update.message.reply_text(f"Đã xóa proxy [{ipv6}]:{port}:{user}:{password}")
            else:
                update.message.reply_text("Proxy không tồn tại!")
            conn.close()
            context.user_data['state'] = None
        except Exception as e:
            logger.error(f"Lỗi khi xóa proxy: {e}")
            update.message.reply_text(f"Định dạng không hợp lệ hoặc lỗi: {e}")
    elif state == 'xoa_all':
        if text == 'Xac_nhan_xoa_all':
            try:
                conn = sqlite3.connect('proxies.db')
                c = conn.cursor()
                c.execute("SELECT ipv6 FROM proxies")
                ipv6_addresses = [row[0] for row in c.fetchall()]
                c.execute("DELETE FROM proxies")
                conn.commit()
                conn.close()
                
                # Xóa tất cả IPv6 khỏi giao diện
                for ipv6 in ipv6_addresses:
                    subprocess.run(['ip', '-6', 'addr', 'del', f'{ipv6}/64', 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                
                open('/etc/squid/passwd', 'w').close()
                
                with open('/etc/squid/squid.conf', 'w') as f:
                    f.write("""
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
""")
                subprocess.run(['systemctl', 'restart', 'squid'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                update.message.reply_text("Đã xóa tất cả proxy!")
                context.user_data['state'] = None
            except Exception as e:
                logger.error(f"Lỗi khi xóa tất cả proxy: {e}")
                update.message.reply_text(f"Lỗi khi xóa tất cả proxy: {e}")
        else:
            update.message.reply_text("Vui lòng nhập: Xac_nhan_xoa_all")

def main():
    init_db()
    # Xóa tất cả IPv6 trên eth0 trước khi chạy
    subprocess.run(['ip', '-6', 'addr', 'flush', 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    subprocess.run(['ip', '-6', 'addr', 'add', f'{IPV6_VPS}/64', 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    
    updater = Updater("7407942560:AAHYBhgVGR_v0nC8PEQ-Ykrg89MMNZ2m_0Q", use_context=True, request_kwargs={'read_timeout': 6, 'connect_timeout': 7, 'con_pool_size': 1})
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CallbackQueryHandler(button))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, message_handler))
    
    # Khởi động luồng kiểm tra proxy
    threading.Thread(target=auto_check_proxies, daemon=True).start()
    
    updater.start_polling(poll_interval=1.0)
    updater.idle()

if __name__ == '__main__':
    main()
