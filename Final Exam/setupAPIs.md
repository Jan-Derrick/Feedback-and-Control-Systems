# 🚀 Nexus Agent API Setup & Deployment Guide

This comprehensive guide walks you through configuring the **Google Calendar API**, **Meta Messenger Developer Webhook**, and **LM Studio** to run the fully automated desktop tracking and notification ecosystem.

---

## 📅 Part 1: Google Calendar API Setup

To log activity blocks and read upcoming schedules, the script needs secure access to your Google Calendar.

### 1. Create a Google Cloud Project
1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Click the project dropdown in the top-left corner and select **New Project**.
3. Name your project (e.g., `Nexus-Productivity-Hub`) and click **Create**.

### 2. Enable the Google Calendar API
1. In the sidebar menu, navigate to **APIs & Services** > **Library**.
2. Search for **Google Calendar API**.
3. Click on it and select **Enable**.

### 3. Configure the OAuth Consent Screen
1. Go to **APIs & Services** > **OAuth consent screen**.
2. Select **External** (or **Internal** if using an institutional Google workspace) and click **Create**.
3. Fill out the required App Info fields (App Name, User support email, Developer contact email).
4. Click **Save and Continue** through the *Scopes* and *Test Users* screens (make sure to add your personal Gmail address as a test user).

### 4. Generate Credentials File
1. Navigate to **APIs & Services** > **Credentials**.
2. Click **+ Create Credentials** at the top and choose **OAuth client ID**.
3. Set the Application type to **Desktop app**.
4. Name it (e.g., `Nexus Desktop Client`) and click **Create**.
5. Find your new credentials listed under *OAuth 2.0 Client IDs*. Click the **Download JSON** icon on the far right.
6. Rename this downloaded file to exactly **`credentials.json`** and move it into your project folder root.

---

## 💬 Part 2: Meta (Facebook Messenger) API Setup

The background service pushes focus alerts and nightly summary cards to your personal Messenger dashboard.

### 1. Create a Meta Developer App
1. Navigate to the [Meta for Developers Portal](https://developers.facebook.com/).
2. Click **My Apps** > **Create App**.
3. Select **Other** > **Business** (or consumer) and click **Next**.
4. Fill out your App Display Name and click **Create App**.

### 2. Set Up Messenger Products
1. Inside your App Dashboard sidebar, scroll down and click **Add Product**.
2. Locate **Messenger** and click **Set Up**.

### 3. Generate the Page Access Token
1. Go to **Messenger** > **API Setup** in the left menu.
2. Under the **Configure Access Tokens** section, click **Create New Page** (or select an existing Facebook Page you manage).
3. Once linked, click **Generate Token**.
4. Copy this massive token string. This is your `FB_PAGE_ACCESS_TOKEN`.

### 4. Locate Your Personal Profile ID (PSID)
Because the bot initiates pushes to your phone directly without you messaging it first, it requires your specific **Page-Scoped ID (PSID)**.
* **Method A (Flask Webhook Logs):** Run your interactive webhook script (`nexus_agent.py`) locally over an `ngrok` tunnel. Message your Facebook Page directly from your personal account: *"Hello Bot"*. Inspect your terminal outputs. Locate the incoming JSON payload and find your unique profile ID array:
  ```json
  "messaging": [{"sender": {"id": "YOUR_PERSONAL_NUMERIC_ID"}}]