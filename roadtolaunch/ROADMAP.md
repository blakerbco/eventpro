# AUCTION INTEL — Road to Launch

## Status Key
- [ ] Not started
- [~] In progress
- [x] Done

---

## 1. CORE PRODUCT (All Done)

- [x] Registration + login + password reset
- [x] Wallet + Stripe payments ($50-$9,999)
- [x] 3-tier billing with tier selection popup
- [x] Research pipeline (3-phase, cache, classify)
- [x] Real-time terminal with SSE progress
- [x] Email validation (Emailable) with purge + terminal visibility
- [x] Result downloads (CSV/JSON/XLSX) persisted to DB
- [x] Research cache (no double-paying for same nonprofit)
- [x] Lead locking ($2.50 exclusive)
- [x] IRS nonprofit database search (up to 10,000 results)
- [x] Support ticket system
- [x] Billing page with full breakdown (locks + refunds)
- [x] Profile page + password change
- [x] Results page with download history (180-day retention)
- [x] Admin tools (field analyzer, value distribution)
- [x] Trial system with promo code (26AUCTION26 = $20 credit)

## 2. RELIABILITY (Just Shipped — Needs Testing)

- [x] SSE reconnect with Last-Event-ID replay
- [x] 15s heartbeats (keeps Railway proxy alive)
- [x] Client auto-reconnect (20 attempts before polling fallback)
- [x] Polling fallback endpoint (/api/job-status)
- [x] Checkpoint saves every 10 items to DB
- [x] Dead loop.close() bug removed
- [ ] **TEST: Run 20+ item batch on Railway, verify SSE reconnect in DevTools**
- [ ] **TEST: Throttle network offline 10s, verify reconnect + catch-up**
- [ ] **TEST: Manually close EventSource in console, verify polling fallback**

## 3. MONEY PIPELINE (Must Verify Before Launch)

- [ ] **Do a real Stripe top-up on auctionintel.app (not localhost)**
  - Verify: card charged
  - Verify: wallet balance updates
  - Verify: transaction appears on billing page
  - Verify: funds receipt email arrives
- [ ] **Run a small paid batch (5-10 nonprofits) on prod**
  - Verify: research fees deduct correctly
  - Verify: lead fees deduct at correct tier prices
  - Verify: billing summary matches wallet transactions
  - Verify: download files contain correct results
- [ ] **Test lead locking on prod**
  - Verify: $2.50 charge + tier refund
  - Verify: shows on billing page

## 4. COMPLETENESSBOT (In Progress)

- [~] Auction type mandatory filter on bot side
  - Only return results with confirmed auction_type: live, silent, or both
  - Unknown/missing auction_type = not_found
- [ ] Run seed list through COMPLETENESSBOT — measure hit rate
- [ ] Compare hit rate: old bot vs COMPLETENESSBOT on same list
- [ ] Decision: acceptable hit rate for launch? (target: 15%+ on known auction orgs)
- [ ] Deploy COMPLETENESSBOT prompt to prod bot.py

## 5. EMAILS (Built — Verify Delivery)

- [x] Welcome email (on registration)
- [x] Job complete email (with results summary)
- [x] Password reset email
- [x] Funds receipt email
- [x] Results expiring emails (10 days, 7 days, 72 hours, 24 hours)
- [x] Ticket created / reply notifications
- [ ] **Verify: Resend actually delivers on prod (check spam folder too)**
- [ ] **Verify: Password reset flow works end-to-end on prod**

## 6. LANDING PAGE + LEGAL

- [ ] Review landing page copy — does it match current product?
  - Pricing tiers accurate?
  - Hit rate claims realistic after COMPLETENESSBOT filter?
  - Screenshots up to date?
- [ ] Terms of Service (even a basic one — you're taking money)
- [ ] Privacy Policy (you're storing emails, payment info)
- [ ] Refund policy clearly stated (non-refundable wallet credits)

## 7. DOMAIN + DNS

- [ ] auctionintel.app pointing to Railway
- [ ] HTTPS working
- [ ] Custom domain verified in Railway dashboard

## 8. NICE-TO-HAVE (Post-Launch)

- [ ] Results expiration cron job (emails are built, needs scheduler)
- [ ] Admin dashboard metrics (revenue, active users, hit rates)
- [ ] Bulk discount tiers for high-volume users
- [ ] API access for power users
- [ ] Webhook notifications on job completion

---

## LAUNCH SEQUENCE (Do These In Order)

1. Finish COMPLETENESSBOT testing, deploy to prod
2. Stripe top-up test on prod
3. Run paid batch on prod (5-10 items)
4. Verify all emails deliver
5. Password reset test on prod
6. Review landing page copy
7. Add ToS + Privacy Policy (even a placeholder)
8. Verify domain + HTTPS
9. **Launch**
