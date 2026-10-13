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
