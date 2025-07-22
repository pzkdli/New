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

# VPS_IPV4 sẽ được nhập qua bot Telegram.
# VPS_IPV6_MAIN sẽ được tự động phát hiện.

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

# Kiểm tra IPv6 có hoạt động trên VPS
def check_ipv6_support():
    try:
        # Thay đổi số lần ping thành 3
        result = subprocess.run(['ping6', '-c', '3', 'ipv6.google.com'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
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
        
        conn = sqlite3.connect('proxies.db')
        c = conn.cursor()
        c.execute("SELECT ipv6 FROM proxies")
        used_ipv6 = [row[0] for row in c.fetchall()]
        conn.close()
        
        ipv6_addresses = []
        for _ in range(num_addresses):
            while True:
                random_host_id = random.getrandbits(64)
                new_ipv6_int = (int(network.network_address) & ( (2**128 - 1) << 64) ) | random_host_id
                ipv6 = str(ipaddress.IPv6Address(new_ipv6_int))
                
                if ipv6 not in used_ipv6 and ipv6 != str(network.network_address) and ipv6 != str(network.broadcast_address):
                    ipv6_addresses.append(ipv6)
                    used_ipv6.append(ipv6)
                    break
        
        return ipv6_addresses
    except Exception as e:
        logger.error(f"Lỗi khi tạo IPv6 từ prefix {prefix}: {e}")
        raise

# Kiểm tra kết nối proxy thực tế
def check_proxy_usage(ipv4_vps, port, user, password, expected_ipv6):
    try:
        # Sử dụng curl -6 để ép buộc kết nối đi bằng IPv6
        cmd = f'curl -6 --interface eth0 --proxy http://{user}:{password}@{ipv4_vps}:{port} --connect-timeout 5 --max-time 10 https://api64.ipify.org?format=json'
        
        # Đặt biến môi trường để Squid biết yêu cầu đến từ IPv6 (nếu cần)
        env = os.environ.copy()
        env['SQUID_REQUEST_VIA_IPV6'] = '1'
        
        result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=15, env=env)
        
        if result.returncode == 0:
            try:
                response = json.loads(result.stdout)
                ip = response.get('ip', '')
                
                if ipaddress.IPv6Address(ip) and ip == expected_ipv6:
                    logger.info(f"Proxy {ipv4_vps}:{port} hoạt động và trả về IPv6 mong muốn: {ip}")
                    return True, ip
                elif ipaddress.IPv6Address(ip) and ip != expected_ipv6:
                    logger.warning(f"Proxy {ipv4_vps}:{port} trả về IPv6: {ip} nhưng không khớp với IPv6 mong muốn: {expected_ipv6}. Có thể là cấu hình bị sai lệch.")
                    return True, ip
                else:
                    logger.warning(f"Proxy {ipv4_vps}:{port} trả về IP: {ip} không phải IPv6 hoặc không khớp.")
                    return False, ip # Trả về False nếu không phải IPv6 hoặc không khớp
            except ValueError:
                logger.warning(f"Proxy {ipv4_vps}:{port} trả về lỗi parse JSON hoặc không có IP: {result.stdout}")
                return False, None
        else:
            logger.error(f"Proxy {ipv4_vps}:{port} không kết nối được hoặc lỗi curl (Exit Code {result.returncode}): {result.stderr}")
            return False, None
    except subprocess.TimeoutExpired:
        logger.error(f"Lỗi: Lệnh kiểm tra proxy {ipv4_vps}:{port} đã hết thời gian.")
        return False, None
    except Exception as e:
        logger.error(f"Lỗi khi kiểm tra proxy {ipv4_vps}:{port}: {e}")
        return False, None

# Tự động kiểm tra proxy mỗi 60 giây
def auto_check_proxies(bot_data): # Sử dụng bot_data thay vì context
    while True:
        try:
            vps_ipv4 = bot_data.get('vps_ipv4') # Lấy VPS_IPV4 từ bot_data
            if not vps_ipv4:
                logger.warning("Không tìm thấy VPS_IPV4 trong auto_check_proxies. Bỏ qua kiểm tra.")
                time.sleep(60)
                continue

            conn = sqlite3.connect('proxies.db')
            c = conn.cursor()
            c.execute("SELECT ipv6, port, user, password FROM proxies")
            proxies_data = c.fetchall()
            conn.close()

            for proxy_info in proxies_data:
                ipv6, port, user, password = proxy_info
                # Kiểm tra proxy, giả định đây là proxy IPv6 ONLY
                is_used, returned_ip = check_proxy_usage(vps_ipv4, port, user, password, ipv6) # Sử dụng vps_ipv4
                
                conn_update = sqlite3.connect('proxies.db')
                c_update = conn_update.cursor()
                # Cập nhật is_used dựa trên kết quả kiểm tra
                c_update.execute("UPDATE proxies SET is_used=? WHERE ipv6=? AND port=? AND user=? AND password=?",
                                 (1 if is_used else 0, ipv6, port, user, password))
                conn_update.commit()
                conn_update.close()
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
        
        proxies_output = []
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
ip_version 6 # Ép buộc Squid ưu tiên IPv6 cho kết nối đi
dns_v4_first off # Không thử phân giải DNS IPv4 trước
http_port 3128
"""
        with open('/etc/squid/squid.conf', 'w') as f:
            f.write(squid_conf_base)
        
        for ipv6 in ipv6_addresses:
            logger.info(f"Gán địa chỉ IPv6 {ipv6}/64 cho eth0.")
            result = subprocess.run(['ip', '-6', 'addr', 'add', f'{ipv6}/64', 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            if result.returncode != 0:
                logger.error(f"Lỗi khi gán IPv6 {ipv6}: {result.stderr}")
                continue
            
            while True:
                port = random.randint(10000, 60000)
                if port not in used_ports:
                    used_ports.append(port)
                    break
            
            user = generate_user()
            password = generate_password()
            expiry_date = (datetime.datetime.now() + datetime.timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
            
            c.execute("INSERT INTO proxies (ipv6, port, user, password, expiry_date, is_used) VALUES (?, ?, ?, ?, ?, ?)",
                      (ipv6, port, user, password, expiry_date, 0))
            
            with open('/etc/squid/squid.conf', 'a') as f:
                f.write(f"\n# Cấu hình cho proxy {user}\n")
                f.write(f"acl proxy_{user} myport {port}\n")
                f.write(f"tcp_outgoing_address {ipv6} proxy_{user}\n")
                f.write(f"http_port {ipv4_vps}:{port}\n") 
            
            result = subprocess.run(['htpasswd', '-b', '/etc/squid/passwd', user, password], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            if result.returncode != 0:
                logger.error(f"Lỗi khi thêm user {user} vào /etc/squid/passwd: {result.stderr}")
                raise Exception(f"Lỗi khi thêm user {user}: {result.stderr}")
            
            proxies_output.append(f"{ipv4_vps}:{port}:{user}:{password}")
        
        conn.commit()
        conn.close()
        
        logger.info("Kiểm tra cấu hình Squid...")
        result = subprocess.run(['squid', '-k', 'check'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if result.returncode != 0:
            logger.error(f"Lỗi cấu hình Squid: {result.stderr}")
            raise Exception(f"Lỗi cấu hình Squid: {result.stderr}")
        
        logger.info("Khởi động lại Squid để áp dụng cấu hình mới...")
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
    
    # Bắt đầu bằng việc hỏi IPv4 của VPS
    update.message.reply_text("Chào bạn! Vui lòng nhập địa chỉ IPv4 của VPS của bạn (ví dụ: 103.1.2.3):")
    context.user_data['state'] = 'ipv4_input'

def button(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
    # Kiểm tra xem VPS_IPV4 đã được thiết lập chưa
    vps_ipv4 = context.bot_data.get('vps_ipv4')
    if not vps_ipv4:
        query.message.reply_text("Vui lòng nhập địa chỉ IPv4 của VPS trước bằng lệnh /start!")
        context.user_data['state'] = 'ipv4_input'
        return

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
                f.write(f"{vps_ipv4}:{p[1]}:{p[2]}:{p[3]}\n") # Sử dụng vps_ipv4
        with open('used.txt', 'w') as f:
            for p in used:
                f.write(f"{vps_ipv4}:{p[1]}:{p[2]}:{p[3]}\n") # Sử dụng vps_ipv4
        
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

    if state == 'ipv4_input':
        try:
            # Validate IPv4 format (basic check)
            ipaddress.IPv4Address(text)
            context.bot_data['vps_ipv4'] = text # Lưu IPv4 vào bot_data (persistent across users/sessions for this bot instance)
            update.message.reply_text("Địa chỉ IPv4 của VPS đã được lưu. Bây giờ, vui lòng nhập prefix IPv6 (định dạng: 2401:2420:0:102f::/64):")
            context.user_data['state'] = 'prefix'
        except ipaddress.AddressValueError:
            update.message.reply_text("Địa chỉ IPv4 không hợp lệ! Vui lòng nhập lại:")
        return # Thoát khỏi hàm sau khi xử lý trạng thái này
    
    # Kiểm tra xem VPS_IPV4 đã được thiết lập chưa trước khi xử lý các lệnh khác
    vps_ipv4 = context.bot_data.get('vps_ipv4')
    if not vps_ipv4:
        update.message.reply_text("Vui lòng nhập địa chỉ IPv4 của VPS trước bằng lệnh /start!")
        context.user_data['state'] = 'ipv4_input'
        return

    if state == 'prefix':
        if validate_ipv6_prefix(text):
            context.user_data['prefix'] = text
            
            # Lấy VPS_IPV6_MAIN đã được phát hiện ở hàm main()
            detected_vps_ipv6_main = context.bot_data.get('vps_ipv6_main_addr')
            if not detected_vps_ipv6_main:
                update.message.reply_text("Không thể xác định địa chỉ IPv6 chính của VPS. Vui lòng kiểm tra cấu hình mạng hoặc khởi động lại bot.")
                context.user_data['state'] = None
                return

            keyboard = [
                [InlineKeyboardButton("/New", callback_data='new'),
                 InlineKeyboardButton("/Xoa", callback_data='xoa')],
                [InlineKeyboardButton("/Check", callback_data='check'),
                 InlineKeyboardButton("/Giahan", callback_data='giahan')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            update.message.reply_text(f"Prefix IPv6 đã được lưu. IPv4 của VPS: {vps_ipv4}. IPv6 chính của VPS: {detected_vps_ipv6_main}. Chọn lệnh:", reply_markup=reply_markup)
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
            if not ipv6_addresses:
                update.message.reply_text("Không thể tạo địa chỉ IPv6. Vui lòng kiểm tra prefix hoặc số lượng đã tạo.")
                return

            proxies = create_proxy(vps_ipv4, ipv6_addresses, days) # Sử dụng vps_ipv4
            
            if not proxies:
                update.message.reply_text("Không có proxy nào được tạo thành công. Vui lòng kiểm tra nhật ký lỗi.")
                return

            if len(proxies) < 5:
                update.message.reply_text("Proxy đã tạo:\n" + "\n".join(proxies))
            else:
                with open('proxies.txt', 'w') as f:
                    for proxy in proxies:
                        f.write(f"{proxy}\n")
                try:
                    context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=open('proxies.txt', 'rb'),
                        caption=f"Đã tạo {len(proxies)} proxy",
                        timeout=30
                    )
                except Exception as e:
                    logger.error(f"Lỗi khi gửi file proxies.txt: {e}")
                    update.message.reply_text(f"Đã tạo {len(proxies)} proxy nhưng lỗi khi gửi file: {e}\nFile proxies.txt đã được lưu trên hệ thống.")
            
            context.user_data['state'] = None
        except Exception as e:
            logger.error(f"Lỗi khi xử lý lệnh /New: {e}")
            update.message.reply_text(f"Định dạng không hợp lệ hoặc lỗi: {e}")
    elif state == 'giahan':
        try:
            proxy_str, days_str = text.rsplit(' ', 1)
            ipv4_from_input, port_str, user, password = proxy_str.split(':')
            port = int(port_str)
            days = int(days_str)

            # Đảm bảo IPv4 trong chuỗi proxy khớp với VPS_IPV4 đã lưu
            if ipv4_from_input != vps_ipv4:
                update.message.reply_text(f"Địa chỉ IPv4 trong proxy ({ipv4_from_input}) không khớp với IPv4 của VPS đã lưu ({vps_ipv4}). Vui lòng kiểm tra lại.")
                return

            conn = sqlite3.connect('proxies.db')
            c = conn.cursor()
            c.execute("SELECT ipv6, expiry_date FROM proxies WHERE port=? AND user=? AND password=?",
                      (port, user, password))
            result = c.fetchone()
            
            if result:
                ipv6_found, old_expiry_str = result
                old_expiry = datetime.datetime.strptime(old_expiry_str, '%Y-%m-%d %H:%M:%S')
                new_expiry = (old_expiry + datetime.timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
                
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
            ipv4_from_input, port_str, user, password = proxy_str.split(':')
            port = int(port_str)
            
            # Đảm bảo IPv4 trong chuỗi proxy khớp với VPS_IPV4 đã lưu
            if ipv4_from_input != vps_ipv4:
                update.message.reply_text(f"Địa chỉ IPv4 trong proxy ({ipv4_from_input}) không khớp với IPv4 của VPS đã lưu ({vps_ipv4}). Vui lòng kiểm tra lại.")
                return

            conn = sqlite3.connect('proxies.db')
            c = conn.cursor()
            c.execute("SELECT ipv6 FROM proxies WHERE port=? AND user=? AND password=?",
                      (port, user, password))
            result = c.fetchone()
            
            if result:
                ipv6_to_delete = result[0]
                c.execute("DELETE FROM proxies WHERE ipv6=?", (ipv6_to_delete,))
                conn.commit()
                
                subprocess.run(['ip', '-6', 'addr', 'del', f'{ipv6_to_delete}/64', 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                
                subprocess.run(['htpasswd', '-D', '/etc/squid/passwd', user], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                
                with open('/etc/squid/squid.conf', 'r') as f:
                    lines = f.readlines()
                with open('/etc/squid/squid.conf', 'w') as f:
                    for line in lines:
                        if (f"acl proxy_{user}" not in line and 
                            f"tcp_outgoing_address {ipv6_to_delete}" not in line and 
                            f"http_port {ipv4_from_input}:{port}" not in line and
                            f"# Cấu hình cho proxy {user}" not in line):
                            f.write(line)
                
                logger.info(f"Khởi động lại Squid sau khi xóa proxy {proxy_str}...")
                subprocess.run(['systemctl', 'restart', 'squid'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                update.message.reply_text(f"Đã xóa proxy {proxy_str} (IPv6: {ipv6_to_delete})")
            else:
                update.message.reply_text("Proxy không tồn tại! Vui lòng kiểm tra lại IPv4 của VPS, Port, User, Pass.")
            conn.close()
            context.user_data['state'] = None
        except Exception as e:
            logger.error(f"Lỗi khi xóa proxy: {e}")
            update.message.reply_text(f"Định dạng không hợp lệ hoặc lỗi: {e}\nVui lòng nhập: ipv4_vps:port:user:pass")
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
                
                # Lấy IPv6 chính của VPS đã được phát hiện
                main_ip_address_only = context.bot_data.get('vps_ipv6_main_addr_only')
                
                for ipv6 in ipv6_addresses:
                    # Chỉ xóa IP phụ, không xóa IP chính của VPS
                    if ipv6 != main_ip_address_only:
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
http_access allow localnet # Sửa từ local_users thành localnet
http_access deny all
auth_param basic program /usr/lib64/squid/basic_ncsa_auth /etc/squid/passwd
auth_param basic children 5
auth_param basic realm Squid Basic Authentication
auth_param basic credentialsttl 2 hours
acl auth_users proxy_auth REQUIRED
http_access allow auth_users
ip_version 6 # Ép buộc Squid ưu tiên IPv6 cho kết nối đi
dns_v4_first off # Không thử phân giải DNS IPv4 trước
http_port 3128
""")
                logger.info("Khởi động lại Squid sau khi xóa tất cả proxy...")
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
    
    # Tự động phát hiện địa chỉ IPv6 chính của VPS
    detected_vps_ipv6_main = None
    try:
        result_show_ipv6 = subprocess.run(['ip', '-6', 'addr', 'show', 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=True, timeout=10)
        logger.info(f"Output of 'ip -6 addr show dev eth0':\n{result_show_ipv6.stdout}") # Log the actual output for debugging
        for line in result_show_ipv6.stdout.splitlines():
            # Điều kiện tìm kiếm IPv6 chính: có 'inet6' và 'scope global'
            if 'inet6' in line and 'scope global' in line:
                parts = line.split()
                for part in parts:
                    if ':' in part and '/' in part: # Looks like an IPv6 address with prefix
                        try:
                            # Validate it's a valid IPv6 network before assigning
                            ipaddress.IPv6Network(part, strict=False)
                            detected_vps_ipv6_main = part
                            break # Found the first valid one, take it.
                        except ValueError:
                            continue # Not a valid IPv6 address with prefix
                if detected_vps_ipv6_main:
                    break # Break outer loop if found
        
        if not detected_vps_ipv6_main:
            logger.error("Không thể tự động phát hiện địa chỉ IPv6 chính của VPS (scope global) từ output: " + result_show_ipv6.stdout)
            # Không throw exception ở đây mà để bot_data['vps_ipv6_main_addr'] là None, và thông báo cho người dùng
    except subprocess.CalledProcessError as e:
        logger.error(f"Lỗi khi chạy lệnh 'ip -6 addr show dev eth0' (Exit Code {e.returncode}): {e.stderr}")
        detected_vps_ipv6_main = None # Ensure it's None if command failed
    except subprocess.TimeoutExpired:
        logger.error("Lệnh 'ip -6 addr show dev eth0' hết thời gian.")
        detected_vps_ipv6_main = None
    except Exception as e:
        logger.error(f"Lỗi không mong muốn khi cố gắng tự động phát hiện IPv6 chính của VPS: {e}")
        detected_vps_ipv6_main = None
    
    if detected_vps_ipv6_main:
        detected_vps_ipv6_main_addr_only = detected_vps_ipv6_main.split('/')[0]
    else:
        detected_vps_ipv6_main_addr_only = None # Fallback nếu không phát hiện được

    # Khởi tạo updater sớm để có thể lưu bot_data
    updater = Updater("7407942560:AAEV5qk3vuPpYN9rZKrxnPQIHteqhh4fQbM", use_context=True, request_kwargs={'read_timeout': 6, 'connect_timeout': 7, 'con_pool_size': 1}) # <-- Thay BOT_TOKEN của bạn vào đây
    dp = updater.dispatcher

    # Lưu địa chỉ IPv6 chính đã phát hiện vào bot_data để các handler khác có thể truy cập
    dp.bot_data['vps_ipv6_main_addr'] = detected_vps_ipv6_main
    dp.bot_data['vps_ipv6_main_addr_only'] = detected_vps_ipv6_main_addr_only


    # Xóa tất cả IPv6 phụ trên eth0 trước khi chạy
    result_flush = subprocess.run(['ip', '-6', 'addr', 'show', 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    current_ipv6s = []
    
    for line in result_flush.stdout.splitlines():
        if 'inet6' in line and 'scope global' in line:
            parts = line.split()
            for part in parts:
                if ':' in part and '/' in part:
                    try:
                        ip_with_prefix = part
                        ip_address = ip_with_prefix.split('/')[0]
                        # Chỉ xóa nếu nó KHÔNG phải là IP chính của VPS
                        if detected_vps_ipv6_main_addr_only and ip_address != detected_vps_ipv6_main_addr_only:
                            current_ipv6s.append(ip_with_prefix)
                        break # Move to next line after finding IP
                    except ValueError:
                        continue
    
    for ip_with_prefix in current_ipv6s:
        logger.info(f"Xóa địa chỉ IPv6 phụ {ip_with_prefix} khỏi eth0.")
        subprocess.run(['ip', '-6', 'addr', 'del', ip_with_prefix, 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    
    # Đảm bảo địa chỉ IPv6 cơ bản (chính) luôn có trên eth0
    if detected_vps_ipv6_main:
        check_main_ip = subprocess.run(['ip', '-6', 'addr', 'show', detected_vps_ipv6_main_addr_only, 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        
        if detected_vps_ipv6_main_addr_only not in check_main_ip.stdout:
            logger.info(f"Thêm địa chỉ IPv6 cơ bản {detected_vps_ipv6_main} cho eth0.")
            subprocess.run(['ip', '-6', 'addr', 'add', detected_vps_ipv6_main, 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        else:
            logger.info(f"Địa chỉ IPv6 cơ bản {detected_vps_ipv6_main} đã tồn tại trên eth0.")
    
    logger.info("Khởi động lại Squid để áp dụng cấu hình ban đầu.")
    result = subprocess.run(['systemctl', 'restart', 'squid'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if result.returncode != 0:
        logger.error(f"Lỗi khi khởi động lại Squid lúc ban đầu: {result.stderr}")
    
    
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CallbackQueryHandler(button))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, message_handler))
    
    # Truyền bot_data cho thread auto_check_proxies
    threading.Thread(target=auto_check_proxies, args=(updater.dispatcher.bot_data,), daemon=True).start()
    
    updater.start_polling(poll_interval=1.0)
    updater.idle()

if __name__ == '__main__':
    main()
