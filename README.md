# 👔 Royal Rental Suits

### Modern Suit Rental Management System

<p align="center">
  <img src="rental/static/logo.png" alt="Royal Rental Logo" width="140">
</p>

<p align="center">
  <strong>A complete Django-based suit rental management platform designed to simplify inventory, customers, rental requests, payments, returns, staff operations, and reporting.</strong>
</p>

<p align="center">
  <a href="#-features">Features</a> •
  <a href="#-technology-stack">Tech Stack</a> •
  <a href="#-user-roles">User Roles</a> •
  <a href="#-installation">Installation</a> •
  <a href="#-project-structure">Structure</a>
</p>

---

## ✨ Overview

**Royal Rental Suits** is a full-featured web application built with **Python and Django** for managing a professional suit rental business.

The system provides separate workflows for **Administrators, Reception Staff, and Customers**, while keeping rental inventory, requests, payments, returns, notifications, and business reports connected through one centralized platform.

The project was developed as a practical software engineering project with a focus on:

* Clean role-based workflows
* Real-world rental business logic
* Inventory management
* Rental lifecycle management
* Payment tracking
* Automated rental expiry handling
* Customer management
* Staff operations
* Reporting and business insights
* Responsive and modern user interfaces

---

## 🎯 Project Goals

The main goal of Royal Rental Suits is to replace manual rental processes with a centralized digital management system.

### The system helps a rental business to:

* Manage suits and rental inventory
* Register and manage customers
* Process rental requests
* Approve and reject rental requests
* Track active rentals
* Monitor rental due dates
* Handle returned suits
* Track payments and expenses
* Manage reception operations
* Monitor inventory conditions
* Generate business reports
* Send customer notifications
* Maintain rental history

---

# 🚀 Features

## 👑 Administrator Dashboard

Administrators have full access to the system and can monitor the entire business.

### Management

* Dashboard overview
* Suit inventory management
* Add, edit and delete suits
* Customer management
* User/staff management
* Rental request management
* Payment management
* Expense management
* Dry-cleaning management
* Notifications
* Reports
* Rental history
* Staff payroll management

---

## 🧾 Reception Management

Reception staff can handle day-to-day rental operations.

### Reception Features

* Reception dashboard
* Register customers
* Create rental requests
* Review rental requests
* Approve rental operations
* Manage active rentals
* Process returns
* Track overdue rentals
* Manage inventory
* Record payments
* Send customer notifications
* View rental reports
* Manage suit condition and cleaning status

---

## 👤 Customer Portal

Customers have access to their own rental information.

### Customer Features

* Customer registration
* Customer login
* Browse available suits
* Submit rental requests
* View booking status
* View rental history
* View rental details
* Receive rental-related notifications

Customers can only access information associated with their own account.

---

# 💰 Rental & Payment Management

The system manages the complete rental lifecycle:

```text
Customer
   ↓
Rental Request
   ↓
Reception Review
   ↓
Admin Approval
   ↓
Active Rental
   ↓
Due Date Tracking
   ↓
Return
   ↓
Payment / Records
   ↓
Rental History
```

The system also supports:

* Rental duration
* Quantity management
* Rental start time
* Rental end time
* Due-date tracking
* Payment verification
* Rental subtotal
* Total rental amount
* Overdue detection
* Return auditing

---

# ⏱️ Rental Expiry & Overdue Management

Royal Rental includes logic for monitoring rental expiration.

The system can identify rentals that have passed their expected return time and mark them as overdue.

This helps staff quickly identify:

* Active rentals
* Rentals approaching expiry
* Overdue rentals
* Returned rentals

A dedicated Django management command is also included for checking expired rentals.

```bash
python manage.py check_expired_rentals
```

---

# 👔 Inventory Management

The inventory system allows staff to manage different suit categories and rental items.

Each suit can contain information such as:

* Suit name
* Collection
* Item type
* Price per day
* Size
* Color
* Quantity
* Status
* Description
* Image
* Rental end time
* Availability

### Example Collections

* Luxury
* Standard
* Budget

---

# 🧼 Suit Condition & Cleaning

The system also supports operational tracking for suit maintenance.

Staff can manage:

* Suit condition
* Cleaning status
* Dry-cleaning records
* Cleaning ledger
* Inventory availability

This allows a rental business to track not only whether an item exists, but whether it is currently ready for another customer.

---

# 📊 Reports & Business Insights

The reporting system provides management with useful business information.

Reports can be used to monitor:

* Rental activity
* Revenue
* Expenses
* Active rentals
* Returned rentals
* Overdue rentals
* Inventory performance
* Business activity

This gives administrators a clearer picture of the business without manually checking individual transactions.

---

# 🔔 Notifications

The application includes notification functionality for rental-related events.

The system contains support for:

* Rental notifications
* Customer reminders
* Email-related workflows
* SMS-related functionality
* Staff notifications

Notification functionality is integrated into the rental workflow so important actions can be communicated to users and staff.

---

# 👥 User Roles

| Role             | Access                                               |
| ---------------- | ---------------------------------------------------- |
| 👑 Administrator | Full system access                                   |
| 🧾 Reception     | Rental operations, customers, inventory and requests |
| 👤 Customer      | Personal bookings and rental history                 |

### Access Philosophy

The application follows role-based access control so that users only access functionality appropriate to their responsibilities.

---

# 🛠️ Technology Stack

## Backend

![Python](https://img.shields.io/badge/Python-3.x-3776AB?style=for-the-badge\&logo=python\&logoColor=white)
![Django](https://img.shields.io/badge/Django-5.x-092E20?style=for-the-badge\&logo=django\&logoColor=white)

* Python
* Django
* Django ORM
* Django Authentication
* Django Templates
* Django Management Commands
* Django Middleware

## Frontend

![HTML5](https://img.shields.io/badge/HTML5-E34F26?style=for-the-badge\&logo=html5\&logoColor=white)
![CSS3](https://img.shields.io/badge/CSS3-1572B6?style=for-the-badge\&logo=css3\&logoColor=white)
![JavaScript](https://img.shields.io/badge/JavaScript-F7DF1E?style=for-the-badge\&logo=javascript\&logoColor=black)

* HTML5
* CSS3
* JavaScript
* Django Templates
* Responsive UI

## Database

![SQLite](https://img.shields.io/badge/SQLite-003B57?style=for-the-badge\&logo=sqlite\&logoColor=white)

* SQLite
* Django ORM
* Database migrations

## Development Tools

![Git](https://img.shields.io/badge/Git-F05032?style=for-the-badge\&logo=git\&logoColor=white)
![GitHub](https://img.shields.io/badge/GitHub-181717?style=for-the-badge\&logo=github\&logoColor=white)
![VS Code](https://img.shields.io/badge/VS%20Code-007ACC?style=for-the-badge\&logo=visual-studio-code\&logoColor=white)

* Git
* GitHub
* Visual Studio Code
* Windows
* Python Virtual Environment

---

# 🏗️ Project Architecture

The project follows Django's application structure with a dedicated rental application.

```text
royal-rental-suits/
│
├── manage.py
│
├── suit_rental/
│   ├── settings.py
│   ├── urls.py
│   ├── asgi.py
│   └── wsgi.py
│
├── rental/
│   ├── migrations/
│   ├── management/
│   │   └── commands/
│   ├── static/
│   ├── templates/
│   │   ├── admin/
│   │   ├── customer/
│   │   ├── reception/
│   │   └── rental/
│   ├── admin.py
│   ├── apps.py
│   ├── decorators.py
│   ├── forms.py
│   ├── middleware.py
│   ├── models.py
│   ├── sms.py
│   ├── urls.py
│   └── views.py
│
├── media/
│   └── suits/
│
├── .gitignore
└── README.md
```

---

# 🔐 Security & Data Protection

The project follows common Django security practices.

Sensitive and generated files are excluded from version control.

Examples:

```text
.env
db.sqlite3
media/customers/
staticfiles/
__pycache__/
*.pyc
```

Customer-uploaded images are intentionally excluded from the public repository.

---

# ⚙️ Installation

## 1. Clone the repository

```bash
git clone https://github.com/zackupdi/royal-rental-suits.git
```

```bash
cd royal-rental-suits
```

---

## 2. Create a virtual environment

### Windows

```bash
python -m venv .venv
```

Activate it:

```bash
.venv\Scripts\activate
```

### Linux / macOS

```bash
python3 -m venv .venv
```

```bash
source .venv/bin/activate
```

---

## 3. Install dependencies

If a `requirements.txt` file is available:

```bash
pip install -r requirements.txt
```

Otherwise install Django:

```bash
pip install django
```

---

## 4. Apply migrations

```bash
python manage.py migrate
```

---

## 5. Create an administrator

```bash
python manage.py createsuperuser
```

Follow the prompts to create the admin account.

---

## 6. Run the development server

```bash
python manage.py runserver
```

Open:

```text
http://127.0.0.1:8000/
```

---

# 🧪 Development Commands

### Check the project

```bash
python manage.py check
```

### Run tests

```bash
python manage.py test
```

### Create migrations

```bash
python manage.py makemigrations
```

### Apply migrations

```bash
python manage.py migrate
```

### Create reception user

```bash
python manage.py create_reception_user
```

### Check expired rentals

```bash
python manage.py check_expired_rentals
```

---

# 📸 Screenshots

> Add your best screenshots here to make the repository visually impressive for recruiters.

### Admin Dashboard

```text
Add screenshot here
```

### Reception Dashboard

```text
Add screenshot here
```

### Inventory Management

```text
Add screenshot here
```

### Customer Portal

```text
Add screenshot here
```

### Rental & Payment Management

```text
Add screenshot here
```

---

# 🎥 Demo

A live demo or demonstration video can be added here.

```text
Coming soon
```

Recommended demo flow:

```text
Login
  ↓
Dashboard
  ↓
Inventory
  ↓
Customer Registration
  ↓
Rental Request
  ↓
Approval
  ↓
Payment
  ↓
Active Rental
  ↓
Return
  ↓
Reports
```

---

# 📈 Future Improvements

Potential future improvements include:

* Online payment integration
* Advanced SMS automation
* Cloud database deployment
* REST API
* Mobile application
* Advanced analytics
* Automated backups
* Multi-branch rental management
* Cloud media storage
* Docker deployment
* Production deployment with PostgreSQL

---

# 🎓 Project Purpose

Royal Rental Suits was developed as a practical software engineering project to demonstrate the design and implementation of a real-world business management system.

The project focuses on transforming manual rental operations into a structured digital workflow.

Through this project, I worked with:

* Backend development
* Database design
* Authentication
* Role-based authorization
* CRUD operations
* Business logic
* Form validation
* File uploads
* Notifications
* Reporting
* Git/GitHub
* Django project architecture

---

# 👨‍💻 Developer

## Zakariya Upsi

**Software Developer | Full-Stack Developer**

I build practical software solutions using modern web and mobile technologies, with a focus on creating systems that solve real-world problems.

### Technical Interests

* Python / Django
* Flutter / Dart
* PHP
* Java
* JavaScript
* HTML / CSS
* MySQL / SQLite
* Firebase
* Git / GitHub

### Connect

📧 **Email:** [zakiupdi111@gmail.com](mailto:zakiupdi111@gmail.com)

🐙 **GitHub:** [github.com/zackupdi](https://github.com/zackupdi)

---

# ⭐ Support

If you find this project useful or interesting, consider giving the repository a ⭐ on GitHub.

---

<p align="center">
  <strong>Built with Python & Django ❤️</strong>
</p>

<p align="center">
  © 2026 Zakariya Upsi — Royal Rental Suits
</p>
