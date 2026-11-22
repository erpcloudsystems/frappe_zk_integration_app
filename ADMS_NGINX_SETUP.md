# ADMS: nginx, Firewall and Testing

This guide covers exposing the ADMS endpoint of `frappe_zk_integration_app` so that a ZKTeco device on another network can push attendance to the server.

ZKTeco devices push over plain **HTTP** to paths under `/iclock/`. The app serves those paths itself through a `page_renderer` hook, so nginx only needs to forward them to Frappe for the right site.

```
[Device] --HTTP--> [Internet] --> [nginx :8081] --> [Frappe :8000] --> Device Log --> Employee Checkin
```

**Prerequisites:** the app is installed on the site (`bench --site <site-name> list-apps`), the site is not in maintenance mode, and you have `sudo` on the server.

---

## 1. nginx

### Why a separate file on a separate port

- `bench setup nginx` and `certbot` regenerate `frappe-bench.conf`, so manual edits there can be lost. A separate file is never touched.
- Most ZK firmware handles HTTPS poorly. If the device used port 80 on the main site, the HTTP→HTTPS redirect would break it.
- The dedicated port exposes `/iclock/` only. Everything else on it returns 404.

### 1.1 Find the upstream name

```bash
grep upstream /etc/nginx/conf.d/*.conf
```

Expected output, which depends on the bench folder name:

```
upstream frappe-bench-frappe {
```

### 1.2 Create the config file

```bash
sudo nano /etc/nginx/conf.d/iclock.conf
```

Replace `<site-name>` and, if different, the upstream name:

```nginx
server {
    listen 8081;
    server_name _;

    access_log /var/log/nginx/iclock.access.log;
    error_log  /var/log/nginx/iclock.error.log;

    client_max_body_size 10m;

    location /iclock/ {
        # If the device site has a static public IP, uncomment to accept only it:
        # allow 41.x.x.x;
        # deny all;

        proxy_pass http://frappe-bench-frappe;
        proxy_http_version 1.1;
        proxy_set_header Host <site-name>;
        proxy_set_header X-Frappe-Site-Name <site-name>;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120;
    }

    # Nothing else is reachable on this port
    location / {
        return 404;
    }
}
```

| Directive | Purpose |
|---|---|
| `listen 8081` | Dedicated port for devices. Any free port works. |
| `proxy_pass http://frappe-bench-frappe` | Forwards to Frappe (gunicorn on 8000). |
| `Host` / `X-Frappe-Site-Name` | Frappe picks the site from these headers. Without them, a device that connects by IP, or a bench with several sites, ends up on the wrong site or gets a 404. |
| `X-Forwarded-For` | Passes the device's real IP to Frappe. |
| `allow` / `deny` | Accepts requests from the device site's IP only. |
| `location / { return 404; }` | Blocks the rest of the site on this port. |

### 1.3 Validate and reload

```bash
sudo nginx -t
sudo systemctl reload nginx
```

Reload only after `nginx -t` reports `syntax is ok` and `test is successful`.

### Alternative: port 80

If the device's network blocks every port except 80/443, put the same `location /iclock/ { ... }` block inside the `listen 80` server block of `/etc/nginx/conf.d/frappe-bench.conf`, **before** certbot's `return 301 https://...` line.

> This edit is lost on every `bench setup nginx` and has to be reapplied.

---

## 2. Firewall

### 2.1 ufw on the server

```bash
sudo ufw allow 8081/tcp

# Or, to accept only the device site's public IP:
# sudo ufw allow from 41.x.x.x to any port 8081 proto tcp

sudo ufw status
```

### 2.2 Cloud provider firewall

On AWS, Hetzner, DigitalOcean and similar providers, also open **TCP 8081** in the provider's **Security Group / Cloud Firewall**. ufw on its own is not enough.

### 2.3 On-prem server behind a router

Add a **port forward** on the router: external TCP 8081 → the server's LAN IP, port 8081. The server needs a static public IP or a DDNS hostname.

---

## 3. Testing

Run these in order. Each step rules out one layer.

### 3.1 On the server (nginx → Frappe)

```bash
curl -i "http://127.0.0.1:8081/iclock/getrequest?SN=TEST"
```

Expected:

```
HTTP/1.1 200 OK
Content-Type: text/plain; charset=utf-8

OK
```

| Result | Cause |
|---|---|
| `Connection refused` | nginx is not listening on 8081. Check `nginx -t` and the reload. |
| `502 Bad Gateway` | Wrong upstream name, or Frappe is down (`bench restart`). |
| `404 Not Found` | Wrong site name in `Host`, or the app is not installed on that site. |
| `503` | The site is in maintenance mode. |
| HTML page instead of `OK` | The request reached a different site. Check `Host` / `X-Frappe-Site-Name`. |

> Test with `getrequest`, **not** `cdata`. A `cdata` request creates a ZK Device named after the SN.

### 3.2 From outside (firewall / port forwarding)

From another machine:

```bash
curl -i "http://<server-public-ip>:8081/iclock/getrequest?SN=TEST"
```

If 3.1 passes and this times out, the problem is the firewall, the Security Group or the port forward.

Confirm the rest of the site is not exposed on this port:

```bash
curl -i "http://<server-public-ip>:8081/app"
# Expected: 404
```

### 3.3 Simulated device (handshake + punch)

This creates a fake device `TEST-ADMS`, sends one punch, and then removes everything.

**Handshake:**

```bash
curl -i "http://<server-public-ip>:8081/iclock/cdata?SN=TEST-ADMS&options=all"
```

Expected body:

```
GET OPTION FROM: TEST-ADMS
Stamp=0
OpStamp=0
ErrorDelay=30
Delay=10
TransTimes=00:00;12:00
TransInterval=1
TransFlag=1000000000
Realtime=1
Encrypt=0
```

**Punch upload.** Columns are TAB-separated: `PIN  DateTime  Status  Verify  WorkCode`.

```bash
curl -i -X POST \
  -H "Content-Type: text/plain" \
  --data-binary $'9999\t2026-10-14 09:00:00\t0\t1\t0' \
  "http://<server-public-ip>:8081/iclock/cdata?SN=TEST-ADMS&table=ATTLOG&Stamp=1"
```

Expected: `OK: 1`

> To test all the way to `Employee Checkin`, replace `9999` with a real employee's Attendance Device ID.

**Verify:**

```bash
bench --site <site-name> console
```

```python
frappe.db.get_value("ZK Device", "TEST-ADMS", ["serial_number", "push_stamp", "last_push_time"], as_dict=True)
frappe.get_all("Device Log", {"device": "TEST-ADMS"}, ["name", "enroll_no", "time", "type", "employee"])
frappe.get_all("Employee Checkin", {"device_id": "TEST-ADMS"}, ["name", "employee", "time", "log_type"])
```

**Clean up:**

```python
for n in frappe.get_all("Employee Checkin", {"device_id": "TEST-ADMS"}, pluck="name"):
    frappe.delete_doc("Employee Checkin", n, force=1)
frappe.db.delete("Device Log", {"device": "TEST-ADMS"})
frappe.delete_doc("ZK Device", "TEST-ADMS", force=1)
frappe.db.commit()
```

### 3.4 Real device

On the device, set **Comm → Cloud Server Setting** to the server's address, port `8081`, HTTPS off. Then watch the log:

```bash
sudo tail -f /var/log/nginx/iclock.access.log
```

Expected:

```
"GET /iclock/cdata?SN=XXXXXXXX&options=all..." 200     <- handshake on connect
"GET /iclock/getrequest?SN=XXXXXXXX..." 200             <- repeats every few seconds
"POST /iclock/cdata?SN=XXXXXXXX&table=ATTLOG..." 200    <- on each punch
```

Punch on the device and confirm:

1. `last_push_time` on the ZK Device has updated.
2. A new `Device Log` exists with the correct time.
3. An `Employee Checkin` exists, if the employee's Attendance Device ID is set.

| Symptom | Fix |
|---|---|
| Nothing in `iclock.access.log` | The device can't reach the server. Check the device's gateway/DNS, the firewall and the port forward. Run 3.2 from the device's network. |
| Requests arrive but return 404/502 | See the table in 3.1. |
| `getrequest` works but no `POST ... ATTLOG` | Restart the device and check its date/time. |
| Still unclear | In Frappe's **Error Log**, look for titles starting with `ZK ADMS`. |
