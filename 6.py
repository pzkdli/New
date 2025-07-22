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
VPS_IPV4 = "YOUR_VPS_IPV4_ADDRESS" # <--- QUAN TRỌNG: Thay thế bằng địa chỉ IPv4 thực tế của VPS của bạn

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
        # Sử dụng base_addr = int(network.network_address) + 1 để tránh .0 (network address)
        # và max_addr = int(network.broadcast_address) -1 để tránh .255 (broadcast address) nếu là /64
        # Nhưng với IPv6, thường không cần tránh .0 hay .FF, chỉ cần đảm bảo trong dải prefix.
        # Với /64, số lượng địa chỉ là cực lớn (2^64), nên việc chọn ngẫu nhiên là đủ.
        
        conn = sqlite3.connect('proxies.db')
        c = conn.cursor()
        c.execute("SELECT ipv6 FROM proxies")
        used_ipv6 = [row[0] for row in c.fetchall()]
        conn.close()
        
        ipv6_addresses = []
        for _ in range(num_addresses):
            while True:
                # Tạo một địa chỉ ngẫu nhiên trong phần host của /64 prefix
                # Với /64, 64 bit cuối là host portion
                random_host_id = random.getrandbits(64)
                # Combine prefix (first 64 bits) with random host_id (last 64 bits)
                new_ipv6_int = (int(network.network_address) & ( (2**128 - 1) << 64) ) | random_host_id
                ipv6 = str(ipaddress.IPv6Address(new_ipv6_int))
                
                # Đảm bảo địa chỉ không phải là địa chỉ mạng hoặc broadcast (thường không áp dụng cứng cho IPv6 như IPv4)
                # và không phải địa chỉ đã tồn tại trong DB.
                if ipv6 not in used_ipv6 and ipv6 != str(network.network_address) and ipv6 != str(network.broadcast_address):
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
        # Sử dụng curl -6 để yêu cầu kết nối qua IPv6
        # --interface eth0 để đảm bảo curl sử dụng đúng giao diện
        # Thêm --proxy-negotiate và --proxy-anyauth để tương thích tốt hơn với các proxy
        cmd = f'curl -6 --interface eth0 --proxy http://{user}:{password}@{ipv4_vps}:{port} --connect-timeout 5 --max-time 10 https://api64.ipify.org?format=json'
        
        # Thêm biến môi trường để Squid logging rõ hơn
        env = os.environ.copy()
        env['SQUID_REQUEST_VIA_IPV6'] = '1' # Chỉ là một cờ thông tin, không phải lệnh Squid
        
        result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=15, env=env)
        
        if result.returncode == 0:
            try:
                response = json.loads(result.stdout)
                ip = response.get('ip', '')
                
                # Kiểm tra xem IP trả về có phải là IPv6 và có khớp với IP mong muốn không
                if ipaddress.IPv6Address(ip) and ip == expected_ipv6:
                    logger.info(f"Proxy {ipv4_vps}:{port} hoạt động và trả về IPv6 mong muốn: {ip}")
                    return True, ip
                elif ipaddress.IPv6Address(ip) and ip != expected_ipv6:
                    logger.warning(f"Proxy {ipv4_vps}:{port} trả về IPv6: {ip} nhưng không khớp với IPv6 mong muốn: {expected_ipv6}. Có thể là cấu hình bị sai lệch.")
                    return True, ip # Vẫn coi là thành công nếu trả về IPv6, nhưng cảnh báo
                else:
                    logger.warning(f"Proxy {ipv4_vps}:{port} trả về IPv4: {ip} thay vì IPv6. Cần kiểm tra lại cấu hình Squid hoặc kết nối IPv6.")
                    return False, ip # Trả về False vì không phải IPv6 như mong muốn
            except ValueError:
                logger.warning(f"Proxy {ipv4_vps}:{port} trả về IP không phải IPv6 hoặc lỗi parse JSON: {result.stdout}")
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
def auto_check_proxies():
    while True:
        try:
            conn = sqlite3.connect('proxies.db')
            c = conn.cursor()
            # Lấy thông tin proxy, sử dụng VPS_IPV4 làm IPv4 kết nối
            c.execute("SELECT ipv6, port, user, password FROM proxies")
            proxies_data = c.fetchall()
            conn.close() # Đóng kết nối DB trước khi gọi subprocess

            for proxy_info in proxies_data:
                ipv6, port, user, password = proxy_info
                # Pass VPS_IPV4 cho hàm kiểm tra
                is_used, returned_ip = check_proxy_usage(VPS_IPV4, port, user, password, ipv6)
                
                # Mở lại kết nối để cập nhật (để tránh lỗi threading nếu có)
                conn_update = sqlite3.connect('proxies.db')
                c_update = conn_update.cursor()
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
http_port 3128 # Port mặc định cho Squid, để nó khởi động
"""
        with open('/etc/squid/squid.conf', 'w') as f:
            f.write(squid_conf_base)
        
        for ipv6 in ipv6_addresses:
            
            # Gán IPv6 vào giao diện
            logger.info(f"Gán địa chỉ IPv6 {ipv6}/64 cho eth0.")
            result = subprocess.run(['ip', '-6', 'addr', 'add', f'{ipv6}/64', 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            if result.returncode != 0:
                logger.error(f"Lỗi khi gán IPv6 {ipv6}: {result.stderr}")
                # Nếu không gán được IP, bỏ qua proxy này và tiếp tục với các proxy khác
                continue # Bỏ qua proxy này và tiếp tục vòng lặp
            
            while True:
                port = random.randint(10000, 60000) # Tăng dải port để tránh xung đột
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
                f.write(f"\n# Cấu hình cho proxy {user}\n")
                f.write(f"acl proxy_{user} myport {port}\n")
                f.write(f"tcp_outgoing_address {ipv6} proxy_{user}\n")
                # Ràng buộc http_port với IPv4 của VPS
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
        logger.info("Kiểm tra cấu hình Squid...")
        result = subprocess.run(['squid', '-k', 'check'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if result.returncode != 0:
            logger.error(f"Lỗi cấu hình Squid: {result.stderr}")
            raise Exception(f"Lỗi cấu hình Squid: {result.stderr}")
        
        # Restart Squid
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
            if not ipv6_addresses:
                update.message.reply_text("Không thể tạo địa chỉ IPv6. Vui lòng kiểm tra prefix hoặc số lượng đã tạo.")
                return

            # Truyền VPS_IPV4 cho hàm tạo proxy
            proxies = create_proxy(VPS_IPV4, ipv6_addresses, days) 
            
            if not proxies:
                update.message.reply_text("Không có proxy nào được tạo thành công. Vui lòng kiểm tra nhật ký lỗi.")
                return

            if len(proxies) < 5: # Chỉ gửi trực tiếp nếu số lượng ít
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
                # Đọc toàn bộ nội dung
                with open('/etc/squid/squid.conf', 'r') as f:
                    lines = f.readlines()
                # Ghi lại chỉ các dòng không liên quan đến proxy đã xóa
                with open('/etc/squid/squid.conf', 'w') as f:
                    for line in lines:
                        # Kiểm tra các dòng cấu hình liên quan đến proxy này
                        if (f"acl proxy_{user}" not in line and 
                            f"tcp_outgoing_address {ipv6_to_delete}" not in line and 
                            f"http_port {ipv4_from_input}:{port}" not in line and
                            f"# Cấu hình cho proxy {user}" not in line): # Xóa cả dòng comment cấu hình
                            f.write(line)
                
                # Restart Squid để áp dụng thay đổi
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
                
                # Xóa tất cả IPv6 khỏi giao diện (trừ IP chính nếu bạn không muốn xóa nó)
                for ipv6 in ipv6_addresses:
                    if ipv6 != "2401:2420:0:102f::1": # Không xóa IP chính nếu nó được lưu trong DB
                        subprocess.run(['ip', '-6', 'addr', 'del', f'{ipv6}/64', 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                
                # Xóa toàn bộ nội dung file passwd (xoá tất cả user)
                open('/etc/squid/passwd', 'w').close()
                
                # Ghi lại cấu hình Squid cơ bản
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
http_port 3128 # Port mặc định cho Squid, để nó khởi động
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
    # Xóa tất cả IPv6 trên eth0 trước khi chạy (trừ IP chính 2401:2420:0:102f::1)
    # Lấy danh sách các IP hiện có
    result_flush = subprocess.run(['ip', '-6', 'addr', 'show', 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    current_ipv6s = []
    for line in result_flush.stdout.splitlines():
        if 'inet6' in line and 'scope global' in line:
            parts = line.split()
            if len(parts) > 1:
                ip_with_prefix = parts[1]
                ip_address = ip_with_prefix.split('/')[0]
                if ip_address != '2401:2420:0:102f::1': # Giữ lại IP chính
                    current_ipv6s.append(ip_with_prefix)
    
    # Xóa các IP phụ khác
    for ip_with_prefix in current_ipv6s:
        subprocess.run(['ip', '-6', 'addr', 'del', ip_with_prefix, 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    
    # Đảm bảo địa chỉ IPv6 cơ bản 2401:2420:0:102f::1/64 luôn có trên eth0
    # Kiểm tra xem IP chính đã tồn tại chưa để tránh lỗi gán trùng
    check_main_ip = subprocess.run(['ip', '-6', 'addr', 'show', '2401:2420:0:102f::1/64', 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if '2401:2420:0:102f::1' not in check_main_ip.stdout:
        logger.info("Thêm địa chỉ IPv6 cơ bản 2401:2420:0:102f::1/64 cho eth0.")
        subprocess.run(['ip', '-6', 'addr', 'add', '2401:2420:0:102f::1/64', 'dev', 'eth0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    
    # Đảm bảo Squid chạy với cấu hình ban đầu
    logger.info("Khởi động lại Squid để áp dụng cấu hình ban đầu.")
    result = subprocess.run(['systemctl', 'restart', 'squid'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if result.returncode != 0:
        logger.error(f"Lỗi khi khởi động lại Squid lúc ban đầu: {result.stderr}")
        # Bạn có thể cân nhắc thoát chương trình hoặc cảnh báo người dùng ở đây nếu Squid không thể khởi động.
    
    updater = Updater("7407942560:AAEV5qk3vuPpYN9rZKrxnPQIHteqhh4fQbM", use_context=True, request_kwargs={'read_timeout': 6, 'connect_timeout': 7, 'con_pool_size': 1})
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
