# ربط أجهزة ZKTeco بطريقة ADMS على سيرفر غير محلي

الدليل ده بيشرح إزاي تربط جهاز بصمة ZKTeco بـ site عليه `frappe_zk_integration_app`، والسيرفر مش على نفس شبكة الجهاز (cloud أو فرع تاني).

---

## الفكرة باختصار

في الطريقة العادية (Polling) السيرفر بيفتح اتصال على الجهاز (port 4370) ويسحب الحركات. ده محتاج السيرفر والجهاز يكونوا على نفس الشبكة أو فيه VPN.

في **ADMS (Push)** الاتجاه بيتعكس: **الجهاز هو اللي بيكلم السيرفر** عن طريق HTTP، ويبعت كل بصمة أول ما تحصل. علشان كده كل اللي محتاجينه:

1. الجهاز يكون عليه إنترنت.
2. السيرفر يتوصله من برّه على port معين.

```
[جهاز البصمة] --HTTP--> [Internet] --> [nginx :8081] --> [Frappe :8000] --> Device Log --> Employee Checkin
```

الجهاز بيستخدم المسارات دي:

| المسار | وظيفته |
|---|---|
| `GET /iclock/cdata?SN=...` | Handshake: الجهاز بيعرّف نفسه والسيرفر بيرد بالإعدادات |
| `POST /iclock/cdata?SN=...&table=ATTLOG` | رفع حركات الحضور |
| `GET /iclock/getrequest?SN=...` | الجهاز بيسأل كل كام ثانية لو فيه أوامر ليه |
| `POST /iclock/devicecmd?SN=...` | نتيجة تنفيذ أمر |

الـ app بيرد على أي مسار يبدأ بـ `/iclock/` عن طريق `page_renderer` (موجود في `adms.py` و `hooks.py`)، فمش محتاجين rewrite ولا method خاص في nginx.

---

## المتطلبات

- Frappe / ERPNext + `hrms` متسطبين على الـ site.
- صلاحية `sudo` على السيرفر.
- الـ site **مش** في maintenance mode، لأن Frappe بيرفض الطلبات وهو في الوضع ده.
- الجهاز بيدعم **Cloud Server Setting / ADMS**. معظم أجهزة ZKTeco الحديثة بتدعمه، وغالباً من قائمة Comm.

---

## 1. تسطيب الـ app

```bash
cd ~/frappe-bench
bench get-app git@github.com:erpcloudsystems/frappe_zk_integration_app
bench --site <site-name> install-app frappe_zk_integration_app
bench --site <site-name> migrate
bench restart
```

اتأكد إن الـ site مش في maintenance mode:

```bash
grep maintenance_mode sites/common_site_config.json sites/<site-name>/site_config.json
# لو طلع 1:
bench --site <site-name> set-maintenance-mode off
```

اتأكد كمان إن الـ **Time Zone** في System Settings هو نفس توقيت الجهاز، لأن الوقت بيتخزن زي ما الجهاز بيبعته بالظبط.

---

## 2. إعداد nginx

### ليه ملف مستقل على port مستقل؟

- `bench setup nginx` و `certbot` بيعيدوا كتابة ملف `frappe-bench.conf`، فأي تعديل فيه ممكن يضيع. الملف المستقل مش هيتلمس.
- الجهاز بيبعت **HTTP** مش HTTPS، ومعظم أجهزة ZK مش بتدعم الشهادات صح. لو حطيناه على port 80 بتاع الـ site، الـ redirect لـ HTTPS هيكسره.
- الـ port ده بيفتح `/iclock/` بس، وباقي الـ site بيرجع 404.

### 2.1 اعرف اسم الـ upstream

```bash
grep upstream /etc/nginx/conf.d/*.conf
```

هيطلع حاجة زي:

```
upstream frappe-bench-frappe {
```

الاسم بيتغير حسب اسم فولدر البنش. استخدم الاسم اللي ظهر عندك في الخطوة الجاية.

### 2.2 اعمل ملف الـ config

```bash
sudo nano /etc/nginx/conf.d/iclock.conf
```

```nginx
server {
    listen 8081;
    server_name _;

    access_log /var/log/nginx/iclock.access.log;
    error_log  /var/log/nginx/iclock.error.log;

    client_max_body_size 10m;

    location /iclock/ {
        # لو موقع الجهاز عنده public IP ثابت، فك الكومنت ده
        # علشان محدش غيره يقدر يبعت حضور:
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

    # أي حاجة غير /iclock/ مقفولة على الـ port ده
    location / {
        return 404;
    }
}
```

شرح السطور المهمة:

| السطر | ليه |
|---|---|
| `listen 8081` | port مخصص للأجهزة. ممكن تختار أي port فاضي. |
| `proxy_pass http://frappe-bench-frappe` | بيوجّه الطلب لـ Frappe (gunicorn على 8000). |
| `Host` و `X-Frappe-Site-Name` | Frappe بيحدد الـ site من الـ headers دي. من غيرهم، لو الجهاز بيكلم السيرفر بالـ IP أو البنش عليه أكتر من site، الطلب هيروح لـ site غلط أو يرجع 404. |
| `X-Forwarded-For` | علشان الـ IP الحقيقي للجهاز يوصل لـ Frappe. |
| `allow` / `deny` | بيقفل الـ endpoint على IP موقع الجهاز بس (موضح في قسم الأمان). |
| `location / { return 404; }` | بيمنع الوصول لباقي الـ site من الـ port ده. |

### 2.3 اختبر الـ config واعمل reload

```bash
sudo nginx -t
sudo systemctl reload nginx
```

`nginx -t` لازم يطلع `syntax is ok` و `test is successful` قبل الـ reload.

---

## 3. فتح الـ port في الـ firewall

```bash
sudo ufw allow 8081/tcp

# أو من IP موقع الجهاز بس:
# sudo ufw allow from 41.x.x.x to any port 8081 proto tcp

sudo ufw status
```

لو السيرفر على cloud (AWS أو Hetzner أو DigitalOcean أو غيرهم)، لازم تفتح 8081 كمان في الـ **Security Group** أو الـ **Firewall** بتاع الـ provider. الـ ufw لوحده مش كفاية.

لو السيرفر on-prem ورا راوتر، اعمل **Port Forwarding** على الراوتر من 8081 الخارجي لـ IP السيرفر الداخلي على 8081.

---

## 4. إعداد الجهاز

من على الجهاز: **Menu → Comm → Cloud Server Setting**. الاسم بيختلف حسب الموديل، وممكن يكون ADMS.

| الإعداد | القيمة |
|---|---|
| Enable Domain Name | `ON` لو هتكتب دومين، `OFF` لو IP |
| Server Address | الـ public IP أو الدومين بتاع السيرفر |
| Server Port | `8081` |
| HTTPS / SSL | `OFF` |
| Enable Proxy Server | `OFF` |

اتأكد كمان من:

- **Comm → Ethernet**: الـ Gateway والـ DNS مظبوطين، والجهاز عليه إنترنت.
- **التاريخ والوقت** على الجهاز صح، ونفس الـ timezone بتاع الـ site.

بعد الحفظ، أيقونة السحابة أو الـ ADMS على شاشة الجهاز المفروض تبقى متوصلة. ممكن تحتاج restart للجهاز.

---

## 5. إعداد البيانات في ERPNext

### ZK Device

عندك اختيارين:

- **تلقائي:** أول handshake من الجهاز بيعمل ZK Device لوحده، اسمه هو الـ Serial Number، و `Auto Attendance` مقفول.
- **يدوي (أفضل):** اعمل ZK Device قبل ما تربط الجهاز:
  - `Device Name`: اسم واضح (مثلاً `Branch-2-Main-Gate`)
  - `Serial Number`: الـ SN بتاع الجهاز (من **Menu → System Info → Device Info**)
  - `Auto Attendance`: **مقفول**

> **مهم:** خلي `Auto Attendance` مقفول على أجهزة ADMS. لو اتفتح، الـ cron اليومي هيحاول يعمل pull من الجهاز بالـ IP، والـ IP ده مش هيكون reachable من السيرفر.

### Employee

لكل موظف املا **Attendance Device ID** بنفس رقم الـ enrollment (User ID) بتاعه على الجهاز. من غيره البصمة هتتسجل في `Device Log` بس من غير `Employee Checkin`.

---

## 6. الاختبار

اعمل الاختبارات بالترتيب ده. كل خطوة بتستبعد سبب من أسباب المشاكل.

### 6.1 من جوه السيرفر (nginx و Frappe)

```bash
curl -i "http://127.0.0.1:8081/iclock/getrequest?SN=TEST"
```

**المتوقع:**

```
HTTP/1.1 200 OK
Content-Type: text/plain; charset=utf-8

OK
```

| النتيجة | السبب |
|---|---|
| `Connection refused` | nginx مش بيسمع على 8081. راجع `nginx -t` والـ reload. |
| `502 Bad Gateway` | اسم الـ upstream غلط أو Frappe واقف (`bench restart`). |
| `404 Not Found` | اسم الـ site في `Host` غلط، أو الـ app مش متسطب على الـ site ده. |
| `503` | الـ site في maintenance mode. |
| صفحة HTML بدل `OK` | الطلب راح لـ site تاني. راجع `Host` و `X-Frappe-Site-Name`. |

> استخدم `getrequest` في الاختبار، **مش** `cdata`، لأن `cdata` بيعمل ZK Device جديد باسم الـ SN.

### 6.2 من برّه السيرفر (firewall و port forwarding)

من جهازك أو أي سيرفر تاني:

```bash
curl -i "http://<server-public-ip>:8081/iclock/getrequest?SN=TEST"
```

لو 6.1 نجح و 6.2 فشل (timeout)، المشكلة في الـ firewall أو الـ Security Group أو الـ port forwarding.

اتأكد كمان إن باقي الـ site مقفول على الـ port ده:

```bash
curl -i "http://<server-public-ip>:8081/app"
# المتوقع: 404
```

### 6.3 محاكاة جهاز كامل (handshake + بصمة)

الاختبار ده بيعمل جهاز وهمي اسمه `TEST-ADMS` ويبعت بصمة، وبعدها بنمسح كل حاجة.

**Handshake:**

```bash
curl -i "http://<server-public-ip>:8081/iclock/cdata?SN=TEST-ADMS&options=all"
```

المتوقع:

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

**إرسال بصمة.** الأعمدة مفصولة بـ TAB: `PIN  DateTime  Status  Verify  WorkCode`

```bash
curl -i -X POST \
  -H "Content-Type: text/plain" \
  --data-binary $'9999\t2026-10-14 09:00:00\t0\t1\t0' \
  "http://<server-public-ip>:8081/iclock/cdata?SN=TEST-ADMS&table=ATTLOG&Stamp=1"
```

المتوقع: `OK: 1`

> لو عايز تختبر لحد `Employee Checkin`، غيّر `9999` لـ Attendance Device ID بتاع موظف حقيقي.

**التحقق:**

```bash
bench --site <site-name> console
```

```python
frappe.db.get_value("ZK Device", "TEST-ADMS", ["serial_number", "push_stamp", "last_push_time"], as_dict=True)
frappe.get_all("Device Log", {"device": "TEST-ADMS"}, ["name", "enroll_no", "time", "type", "employee"])
frappe.get_all("Employee Checkin", {"device_id": "TEST-ADMS"}, ["name", "employee", "time", "log_type"])
```

**التنظيف:**

```python
for n in frappe.get_all("Employee Checkin", {"device_id": "TEST-ADMS"}, pluck="name"):
    frappe.delete_doc("Employee Checkin", n, force=1)
frappe.db.delete("Device Log", {"device": "TEST-ADMS"})
frappe.delete_doc("ZK Device", "TEST-ADMS", force=1)
frappe.db.commit()
```

### 6.4 مع الجهاز الحقيقي

تابع الـ log على السيرفر:

```bash
sudo tail -f /var/log/nginx/iclock.access.log
```

المفروض تشوف:

```
"GET /iclock/cdata?SN=XXXXXXXX&options=all..." 200     <- handshake أول ما يتوصل
"GET /iclock/getrequest?SN=XXXXXXXX..." 200             <- بيتكرر كل كام ثانية
"POST /iclock/cdata?SN=XXXXXXXX&table=ATTLOG..." 200    <- لما حد يبصم
```

بعدها ابصم على الجهاز واتأكد إن:

1. `last_push_time` في الـ ZK Device اتحدث.
2. فيه `Device Log` جديد بالوقت الصح.
3. فيه `Employee Checkin` للموظف، لو الـ Attendance Device ID بتاعه متظبط.

> أول ما الجهاز يتوصل ممكن يرفع كل الحركات القديمة اللي عليه مرة واحدة. ده طبيعي، والـ app بيتجاهل أي حركة متكررة.

---

## 7. حل المشاكل

| المشكلة | الحل |
|---|---|
| مفيش أي حاجة في `iclock.access.log` | الجهاز مش واصل للسيرفر. راجع إعدادات الشبكة على الجهاز (Gateway و DNS)، والـ firewall، والـ port forwarding. جرّب 6.2 من نفس شبكة الجهاز. |
| الطلبات بتوصل بس بترجع 404 أو 502 | ارجع لجدول 6.1. |
| `getrequest` شغال بس مفيش `POST ... ATTLOG` | الجهاز مش بيرفع الحركات. اعمل restart للجهاز، واتأكد إن التاريخ والوقت صح. |
| `Device Log` بيتعمل بس مفيش `Employee Checkin` | `Attendance Device ID` مش متظبط على الموظف، أو فيه مسافة زيادة. البصمات اللي جاية بعد كده هتتربط تلقائي. |
| الوقت متزحزح بساعات | الـ timezone على الجهاز مختلف عن System Settings. |
| مشكلة مش واضحة | افتح **Error Log** في Frappe ودوّر على عناوين بتبدأ بـ `ZK ADMS`. الـ app بيسجل فيها أي path مش متوقع أو أي exception. |

---

## 8. ملاحظات أمان

- الـ endpoint مفتوح للـ Guest، **وأي SN مش معروف بيتعمله ZK Device جديد تلقائي**. يعني أي حد يعرف الـ IP والـ port يقدر يبعت حضور مزيف.
- **أقل حاجة:** لو موقع الجهاز عنده public IP ثابت، فعّل `allow` / `deny` في nginx وقيّد `ufw` على الـ IP ده.
- لو الـ IP مش ثابت، الأفضل تعمل VPN بين موقع الجهاز والسيرفر، أو تعدل الـ app يرفض أي SN مش متسجل يدوياً.

---

## 9. بديل: استخدام port 80

بعض الشبكات بتقفل أي port غير 80 و 443. في الحالة دي حط الـ `location /iclock/` نفسه (من خطوة 2.2) جوه الـ `server` block بتاع `listen 80` في `/etc/nginx/conf.d/frappe-bench.conf`، **قبل** سطر `return 301 https://...` اللي عمله certbot.

> التعديل ده هيضيع مع أي `bench setup nginx`، ولازم تعيده بعدها.

---

## 10. ملاحظة عن IN / OUT

نوع الحركة بيتحدد **بالساعة**، مش بزرار الجهاز: قبل 12 الضهر `IN` وبعدها `OUT`. ده مش هيظبط مع الشيفتات الليلية أو اللي بتعدي نص اليوم.
