async def allusers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized.")
        return
    if not all_users:
        await update.message.reply_text("No users yet.")
        return
    msg = "*All Users:*\n\n"
    for uid, info in all_users.items():
        username = info.get("username") or "N/A"
        first_name = info.get("first_name") or "N/A"
        msg += f"ID: `{uid}` | {username} | {first_name}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")
