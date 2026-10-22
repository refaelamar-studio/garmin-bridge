# Garmin Connect TLS Bridge Service

שירות עזר עצמאי (Microservice) מבוסס Python המבצע התחזות TLS (Safari/iOS) עבור Garmin Connect SSO, ומאפשר אימות וסנכרון אימונים מלא גם בסביבות ענן מופצות (Production).

## אופציות פריסה (Deployment Options)

### אופציה 1: פריסה קלה ב-Render / Railway / Koyeb (חינם או $3-5/חודש)
1. צור חשבון ב-[Render.com](https://render.com) או [Railway.app](https://railway.app).
2. בחר **New Web Service** והעלה את התיקייה הזו (`server/garmin-bridge-deploy`).
3. הפלטפורמה תבנה את ה-Dockerfile אוטומטית.
4. בסיום תקבל כתובת URL כגון: `https://my-garmin-bridge.onrender.com`
5. הגדר בהגדרות הסביבה של האפליקציה (Cloud Run / AI Studio):
   ```env
   GARMIN_BRIDGE_URL=https://my-garmin-bridge.onrender.com
   ```

### אופציה 2: הרצה על שרת VPS פרטי עם Docker
```bash
docker build -t garmin-bridge .
docker run -d -p 5055:5055 --restart always --name garmin-bridge garmin-bridge
```

## בדיקת תקינות (Health Check)
```bash
curl https://<YOUR_BRIDGE_URL>/health
# תגובה: {"status": "ok", "service": "garmin-bridge"}
```
