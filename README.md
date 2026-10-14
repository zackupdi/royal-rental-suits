# Modern Suit Rental Management System

<p align="center">
  <img src="rental/static/logo.png" alt="Royal Rental Logo" width="140">
</p>

<p align="center">
  <strong>A modern, secure, and complete Django-based platform built to simplify and manage the entire suit rental business lifecycle.</strong>
</p>

---

## Overview

**Royal Rental** is a complete **Suit Rental Management System** designed to help rental businesses efficiently manage their daily operations from one centralized platform.

The system provides a modern workflow for managing **suits, customers, rental requests, payments, returns, inventory, notifications, and business reports**.

It is designed with a focus on **simplicity, security, automation, and a professional user experience**, making it suitable for both small and growing suit rental businesses.

### Key Capabilities

* 👔 Suit & Inventory Management
* 👤 Customer Management
* 📋 Rental Request Management
* 💳 Payment & Financial Tracking
* 🔄 Rental Returns & Status Tracking
* 👨‍💼 Admin & Customer Accounts
* ⏱️ Rental Expiry & Overdue Tracking
* 🧹 Suit Cleaning & Availability Management
* 📊 Business Reports & Analytics
* 🔔 Notifications & Rental Updates
* 📱 Customer Rental History
* 🔐 Role-Based Access Control

> **Royal Rental brings the complete rental workflow into one organized platform — from registering a customer and requesting a suit to approval, payment, return, and reporting.**

## Repeat rentals

Staff can open an existing customer by account ID, review that customer's rental and payment history, and create another rental without creating a duplicate customer account. Each booking remains a separate rental with its own payments, return status, and balance. An active or overdue rental is shown as a warning but does not block another booking when inventory is available.

Reception access is limited to the customer directory and repeat-rental workflow; administrative inventory, payment, and reporting pages remain restricted. These workflows use the existing customer, rental, and payment relationships and do not require a schema migration.

## Deploying on Render

The repository includes a Render Blueprint (`render.yaml`) and build script. Create a Render PostgreSQL database in the same region as the web service, then set the web service's `DATABASE_URL` to that database's **internal** connection URL. The Blueprint generates `DJANGO_SECRET_KEY`; do not replace it with a value committed to Git.

Set these required environment variables in Render:

* `DEBUG=false`
* `DATABASE_URL` — the Render PostgreSQL internal connection URL
* `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, and `CLOUDINARY_API_SECRET` — Cloudinary credentials for persistent uploads

Static files are collected during the build and served by WhiteNoise. Render's local filesystem is temporary, so uploaded media is stored in Cloudinary. To copy the existing **suit catalog images** before deployment, set the three Cloudinary variables in the local PowerShell session and run:

```powershell
$env:CLOUDINARY_CLOUD_NAME = "<Cloudinary cloud name>"
$env:CLOUDINARY_API_KEY = "<Cloudinary API key>"
$env:CLOUDINARY_API_SECRET = "<Cloudinary API secret>"
python manage.py sync_suit_images
```

The command only copies files from `media/suits/`. Customer photos, profile photos, and ID uploads use authenticated Cloudinary storage and signed URLs rather than public image URLs. To preserve existing private uploads too, run:

```powershell
python manage.py sync_private_uploads
```

### Preserve existing accounts and rental data

The SQLite database is not part of Git. First run the media sync commands above against the local database so existing image paths are updated. Then make a private fixture backup from the computer that has the current `db.sqlite3`:

```powershell
python manage.py dumpdata --natural-foreign --natural-primary --exclude=contenttypes --exclude=auth.permission --output=render-data.json
```

Deploy the Render service first so that the build applies database migrations. Then use the Render database's **external** URL temporarily from your trusted computer to load the backup:

```powershell
$env:DEBUG = "false"
$env:DJANGO_SECRET_KEY = "<the value generated in Render>"
$env:DATABASE_URL = "<Render external database URL>"
$env:CLOUDINARY_CLOUD_NAME = "<Cloudinary cloud name>"
$env:CLOUDINARY_API_KEY = "<Cloudinary API key>"
$env:CLOUDINARY_API_SECRET = "<Cloudinary API secret>"
python manage.py loaddata render-data.json
```

This transfers Django users and their password hashes as well as the rental data, so existing usernames and passwords remain valid. Keep the fixture private, never commit it or share it, and delete it after confirming the import. File uploads are not included in Django fixtures; sync suit catalog images separately as described above.

After deployment, use the Render service URL to sign in with an existing staff account. If no staff account exists in the imported data, create one with `python manage.py createsuperuser` against the Render database.

Rotate any credentials that were previously stored in source code, and enter replacement credentials only in Render's environment settings. Never commit `.env` files, database exports, or Cloudinary credentials.
