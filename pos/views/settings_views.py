"""Settings page (company, team, roles, backups) and backup download."""
import json
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError
from django.db.models import Count, Sum
from django.db.models.functions import Coalesce
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.forms import BankAccountForm
from accounts.models import Account, AuditLog, BackupSnapshot, BankAccount
from accounts.services.audit import record_audit
from accounts.services.backup_restore import (
    backup_runtime_summary,
    create_backup_snapshot,
    database_vendor_label,
    get_backup_file_path,
    infer_backup_vendor,
    restore_backup_snapshot,
    snapshot_restore_block_reason,
    upload_backup_snapshot,
)
from ..forms import (
    AdminTimeZoneForm,
    CompanyProfileForm,
    CustomRoleForm,
    SystemSettingsForm,
    TeamUserForm,
)
from ..models import (
    Category,
    CustomRole,
    UserPreference,
    UserSessionLog,
)
from ..services.access import (
    ACCESS_MANAGE,
    MODULE_DEFINITIONS,
    admin_required,
    assign_role,
    describe_permissions,
    get_user_role_value,
    ensure_role_groups,
    feature_required,
    get_role_choices,
    get_role_label,
    get_user_role_label,
    get_user_role_summary,
    is_workspace_owner,
    open_user_session,
)
from ..services.user_timezones import (
    get_effective_time_zone,
    get_session_time_zone,
    get_user_time_zone_preference,
)
from ..services.billing import user_can_manage_billing, workspace_owner_onboarding
from ..services.licensing import team_user_limit_message
from ..services.modules import (
    FEATURE_MODULES,
    ModuleError,
    edition_summary,
    features_overview,
    get_module_state,
    module_disabled_message,
    module_enabled,
    module_within_ceiling,
    set_module_switch,
)
from ..services.throttling import (
    RESTORE_PASSWORD_SCOPE,
    attempt_count,
    clear_attempts,
    client_ip,
    register_attempt,
)
from ..services.business_rules import get_active_company_profile, get_system_settings
from ._shared import _remember_browser_time_zone
from .billing import _billing_page_context


def _lan_till_urls():
    """Addresses other tills on the shop network can open; the desktop launcher writes them at start-up."""
    if getattr(settings, "DEPLOYMENT_MODE", "") != "desktop":
        return []
    try:
        payload = json.loads((Path(settings.DATA_DIR) / "zehnms.lock").read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError, TypeError):
        return []
    urls = payload.get("lan_urls") if isinstance(payload, dict) else None
    return [str(url) for url in urls or [] if isinstance(url, str)]


def _handle_save_features(request):
    """Settings > Features. The licence ceiling is enforced here, not by the greyed-out switch."""
    state = get_module_state()
    changed = {}
    refused = []
    for module in FEATURE_MODULES:
        wanted = bool(request.POST.get(f"module_{module.key}"))
        if not state.within_ceiling(module.key):
            # A locked switch is never submitted by the page, so this is a hand-made request.
            if wanted:
                refused.append(module.label)
            continue
        if wanted == (module.key in state.enabled):
            continue
        try:
            set_module_switch(module.key, wanted)
        except ModuleError:
            refused.append(module.label)
            continue
        changed[module.key] = wanted

    if changed.get("tax"):
        Account.objects.get_or_create(title="Sales Tax Payable", defaults={"type": Account.LIABILITY})
    if changed:
        labels = {module.key: module.label for module in FEATURE_MODULES}
        record_audit(
            action="feature_modules_updated",
            description="Features switched: "
            + ", ".join(f"{labels[key]} {'on' if value else 'off'}" for key, value in changed.items())
            + ".",
            actor=request.user,
            target=get_system_settings(),
            metadata={"changed": changed},
        )
        messages.success(request, "Features updated.")
    elif not refused:
        messages.info(request, "No feature was changed.")
    for label in refused:
        messages.error(request, f"{label} is not included in your edition, so it stays off.")
    return redirect(f"{reverse('settings_page')}?tab=features")


# Settings actions that belong to a feature module; refused for everyone while it is off.
ACTION_MODULES = {
    "create_user": "team",
    "create_role": "team",
    "create_backup": "backups",
    "upload_backup": "backups",
    "restore_backup": "backups",
}

RESTORE_PASSWORD_MAX_ATTEMPTS = 5
RESTORE_PASSWORD_WINDOW_SECONDS = 900


def _render_restore_confirmation(request, snapshot, *, password_error=""):
    return render(
        request,
        "pos/backup_restore_confirm.html",
        {
            "page_title": "Restore Backup",
            "page_subtitle": "Replace the live data with a saved snapshot.",
            "page_icon": "bi-arrow-counterclockwise",
            "page_icon_accent": "slate",
            "snapshot": snapshot,
            "password_error": password_error,
            "has_usable_password": request.user.has_usable_password(),
        },
    )


def _continue_session_after_restore(request, password):
    """Sessions live in the database that was just replaced, so the old one is gone.

    The admin keeps working without a detour through the login page when the same
    account, with the same password, exists in the restored data.
    """
    restored_user = (
        get_user_model().objects.filter(pk=request.user.pk, username=request.user.get_username(), is_active=True).first()
    )
    request.session.flush()
    if restored_user is None or not restored_user.check_password(password):
        return False
    login(request, restored_user)
    open_user_session(request, restored_user)
    return True


def _handle_restore_backup(request):
    snapshot = get_object_or_404(BackupSnapshot, pk=request.POST.get("snapshot_id"))
    block_reason = snapshot_restore_block_reason(snapshot)
    if block_reason:
        messages.error(request, block_reason)
        return redirect("settings_page")

    password = request.POST.get("confirm_password")
    if password is None:
        # First click: ask for the password on one focused screen, then restore.
        return _render_restore_confirmation(request, snapshot)

    throttle_key = f"user:{request.user.pk}"
    if attempt_count(RESTORE_PASSWORD_SCOPE, throttle_key) >= RESTORE_PASSWORD_MAX_ATTEMPTS:
        messages.error(request, "Too many wrong passwords. Restoring is paused for about 15 minutes.")
        return redirect("settings_page")

    if not request.user.has_usable_password() or not request.user.check_password(password):
        attempts = register_attempt(RESTORE_PASSWORD_SCOPE, throttle_key, window_seconds=RESTORE_PASSWORD_WINDOW_SECONDS)
        record_audit(
            action="backup_restore_denied",
            description=f"Restore of backup '{snapshot.label}' was refused: wrong password confirmation.",
            actor=request.user,
            target=snapshot,
            metadata={"attempts": attempts, "ip_address": client_ip(request)},
        )
        return _render_restore_confirmation(
            request,
            snapshot,
            password_error="That password is not correct. Nothing was restored.",
        )

    clear_attempts(RESTORE_PASSWORD_SCOPE, throttle_key)
    label = snapshot.label
    try:
        restored = restore_backup_snapshot(snapshot=snapshot, actor=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("settings_page")
    except DatabaseError:
        messages.error(
            request,
            f"Backup '{label}' could not be restored cleanly. The safety backup taken just before is in the backup list.",
        )
        return redirect("settings_page")

    try:
        still_signed_in = _continue_session_after_restore(request, password)
    except DatabaseError:
        still_signed_in = False

    messages.success(request, f"Backup '{label}' restored. A safety backup of the previous data was saved first.")
    for problem in getattr(restored, "follow_up_problems", []):
        messages.warning(request, problem)
    if not still_signed_in:
        messages.info(request, "Sign in with an account from the restored data to continue.")
        return redirect("login")
    return redirect("settings_page")


@admin_required
def settings_page(request):
    ensure_role_groups()
    user_model = get_user_model()
    settings_obj = get_system_settings()
    company_profile = get_active_company_profile(create=True)
    try:
        time_zone_preference = request.user.preference
    except UserPreference.DoesNotExist:
        time_zone_preference = UserPreference(user=request.user)
    detected_time_zone = get_session_time_zone(request)
    effective_time_zone = getattr(
        request,
        "effective_time_zone",
        get_effective_time_zone(request.user, session_time_zone=detected_time_zone),
    )

    team_form = TeamUserForm(prefix="team")
    role_form = CustomRoleForm(prefix="role")
    branding_form = CompanyProfileForm(prefix="branding", instance=company_profile)
    system_form = SystemSettingsForm(prefix="business", instance=settings_obj)
    time_zone_form = AdminTimeZoneForm(
        prefix="timezone",
        instance=time_zone_preference,
        detected_time_zone=detected_time_zone,
        effective_time_zone=effective_time_zone,
    )
    bank_form = BankAccountForm(prefix="bank")
    backup_runtime = backup_runtime_summary()
    audit_page_size = 10

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "save_features":
            return _handle_save_features(request)

        action_module = ACTION_MODULES.get(action)
        if action_module and not module_enabled(action_module):
            messages.error(request, module_disabled_message(action_module))
            return redirect("settings_page")

        if action == "create_user":
            team_form = TeamUserForm(request.POST, prefix="team")
            limit_message = team_user_limit_message() if request.POST.get("team-is_active") else ""
            if limit_message:
                messages.error(request, limit_message)
            elif team_form.is_valid():
                member = team_form.save()
                role_value = team_form.cleaned_data["role"]
                role_label = get_role_label(role_value)
                assign_role(member, role_value)
                record_audit(
                    action="team_user_created",
                    description=f"Team member '{member.get_full_name() or member.username}' created from Settings.",
                    actor=request.user,
                    target=member,
                    metadata={"role": role_label},
                )
                messages.success(request, f"Team member '{member.get_full_name() or member.username}' created.")
                return redirect("settings_page")
            else:
                messages.error(request, "Please review the team member details below.")

        elif action == "create_role":
            role_form = CustomRoleForm(request.POST, prefix="role")
            if role_form.is_valid():
                role = role_form.save()
                record_audit(
                    action="custom_role_created",
                    description=f"Custom role '{role.name}' created from Settings.",
                    actor=request.user,
                    target=role,
                    metadata={"permissions": role.permissions},
                )
                messages.success(request, f"Custom role '{role.name}' is ready to assign.")
                return redirect("settings_page")
            messages.error(request, "Please review the custom role details below.")

        elif action == "save_time_zone":
            _remember_browser_time_zone(request, request.POST.get("browser_time_zone"))
            time_zone_form = AdminTimeZoneForm(
                request.POST,
                prefix="timezone",
                instance=time_zone_preference,
                detected_time_zone=get_session_time_zone(request),
                effective_time_zone=effective_time_zone,
            )
            if time_zone_form.is_valid():
                preference = time_zone_form.save(commit=False)
                preference.user = request.user
                preference.save()
                saved_time_zone = time_zone_form.cleaned_data["time_zone"]
                effective_label = saved_time_zone or get_session_time_zone(request) or settings.TIME_ZONE
                record_audit(
                    action="admin_time_zone_updated",
                    description=(
                        f"Time zone preference updated for {request.user.get_full_name() or request.user.username}."
                    ),
                    actor=request.user,
                    target=request.user,
                    metadata={
                        "time_zone": saved_time_zone or "system_detected",
                        "effective_time_zone": effective_label,
                    },
                )
                if saved_time_zone:
                    messages.success(request, f"Time zone saved as {saved_time_zone}.")
                else:
                    messages.success(request, f"Time zone will follow the detected system zone ({effective_label}).")
                return redirect("settings_page")
            messages.error(request, "Please review the time zone setting below.")

        elif action == "change_user_role":
            member = get_object_or_404(user_model, pk=request.POST.get("user_id"))
            selected_role = (request.POST.get("role") or "").strip()
            available_roles = {value for value, _ in get_role_choices()}

            if member.id == request.user.id:
                messages.error(request, "You cannot change the role of the account you are currently using.")
            elif is_workspace_owner(member):
                messages.error(request, "The owner account always keeps full control and its role cannot be changed.")
            elif selected_role not in available_roles:
                messages.error(request, "The selected role is no longer available.")
            else:
                assign_role(member, selected_role)
                role_label = get_role_label(selected_role)
                UserSessionLog.objects.filter(user=member, logout_at__isnull=True).update(role_name=role_label)
                record_audit(
                    action="team_user_role_changed",
                    description=f"Role changed for {member.get_full_name() or member.username} to {role_label}.",
                    actor=request.user,
                    target=member,
                    metadata={"role": role_label},
                )
                messages.success(request, f"Role updated for {member.get_full_name() or member.username}.")
                return redirect("settings_page")

        elif action == "save_branding":
            branding_form = CompanyProfileForm(request.POST, request.FILES, prefix="branding", instance=company_profile)
            if branding_form.is_valid():
                branding = branding_form.save(commit=False)
                branding.is_active = True
                branding.save()
                record_audit(
                    action="branding_updated",
                    description="Business branding and receipt identity were updated.",
                    actor=request.user,
                    target=branding,
                )
                messages.success(request, "Business branding and receipt details updated.")
                return redirect("settings_page")
            messages.error(request, "Please review the branding details below.")

        elif action == "save_rules":
            system_form = SystemSettingsForm(request.POST, prefix="business", instance=settings_obj)
            if system_form.is_valid():
                settings_obj = system_form.save()
                if settings_obj.gst_enabled:
                    Account.objects.get_or_create(title="Sales Tax Payable", defaults={"type": Account.LIABILITY})
                record_audit(
                    action="system_settings_updated",
                    description="Tax and POS rules were updated from Settings.",
                    actor=request.user,
                    target=settings_obj,
                    metadata={
                        "gst_enabled": settings_obj.gst_enabled,
                        "tax_mode": settings_obj.tax_mode,
                        "tax_label": settings_obj.tax_label,
                    },
                )
                messages.success(request, "Tax and POS rules updated.")
                return redirect("settings_page")
            messages.error(request, "Please review the settings below.")

        elif action == "create_bank":
            bank_form = BankAccountForm(request.POST, prefix="bank")
            if bank_form.is_valid():
                bank = bank_form.save()
                record_audit(
                    action="bank_ledger_created",
                    description=f"Bank ledger '{bank.display_name}' created.",
                    actor=request.user,
                    target=bank,
                )
                messages.success(request, f"Bank ledger '{bank.display_name}' created.")
                bank_form = BankAccountForm(prefix="bank")
            else:
                messages.error(request, "Please review the bank ledger details below.")

        elif action == "toggle_bank":
            bank = get_object_or_404(BankAccount, pk=request.POST.get("bank_id"))
            bank.is_active = not bank.is_active
            bank.save(update_fields=["is_active"])
            record_audit(
                action="bank_ledger_toggled",
                description=f"Bank ledger '{bank.display_name}' {'enabled' if bank.is_active else 'disabled'}.",
                actor=request.user,
                target=bank,
                metadata={"is_active": bank.is_active},
            )
            messages.success(request, f"{bank.display_name} {'enabled' if bank.is_active else 'disabled'}.")

        elif action == "create_backup":
            label = (request.POST.get("backup_label") or "").strip()
            notes = (request.POST.get("backup_notes") or "").strip()
            try:
                snapshot = create_backup_snapshot(label=label, notes=notes, actor=request.user)
                messages.success(request, f"Backup '{snapshot.label}' created.")
            except ValueError as exc:
                messages.error(request, str(exc))

        elif action == "upload_backup":
            label = (request.POST.get("upload_backup_label") or "").strip()
            notes = (request.POST.get("upload_backup_notes") or "").strip()
            uploaded_file = request.FILES.get("backup_upload_file")
            try:
                snapshot = upload_backup_snapshot(uploaded_file=uploaded_file, label=label, notes=notes, actor=request.user)
                messages.success(request, f"Backup '{snapshot.label}' uploaded.")
            except ValueError as exc:
                messages.error(request, str(exc))

        elif action == "restore_backup":
            return _handle_restore_backup(request)

        elif action == "toggle_user":
            member = get_object_or_404(user_model, pk=request.POST.get("user_id"))
            if member.id == request.user.id:
                messages.error(request, "You cannot disable the account you are currently using.")
            elif is_workspace_owner(member):
                messages.error(request, "The owner account cannot be disabled.")
            elif not member.is_active and team_user_limit_message():
                messages.error(request, team_user_limit_message())
            else:
                member.is_active = not member.is_active
                member.save(update_fields=["is_active"])
                if not member.is_active:
                    UserSessionLog.objects.filter(user=member, logout_at__isnull=True).update(logout_at=timezone.now())
                status_copy = "enabled" if member.is_active else "disabled"
                record_audit(
                    action="team_user_toggled",
                    description=f"{member.get_full_name() or member.username} was {status_copy} from Settings.",
                    actor=request.user,
                    target=member,
                    metadata={"is_active": member.is_active},
                )
                messages.success(request, f"{member.get_full_name() or member.username} {status_copy}.")

        elif action == "set_user_password":
            member = get_object_or_404(user_model, pk=request.POST.get("user_id"))
            new_password = request.POST.get("new_password") or ""
            member_name = member.get_full_name() or member.username
            if member.id == request.user.id:
                # Changing your own password asks for the current one first.
                return redirect("password_change")
            if is_workspace_owner(member) or member.is_superuser:
                messages.error(request, "Only the owner can change the owner's password.")
            else:
                try:
                    validate_password(new_password, user=member)
                except ValidationError as exc:
                    messages.error(request, f"Password for {member_name} was not changed. " + " ".join(exc.messages))
                else:
                    member.set_password(new_password)
                    member.save(update_fields=["password"])
                    # The old password no longer opens anything, including sessions already running.
                    UserSessionLog.objects.filter(user=member, logout_at__isnull=True).update(logout_at=timezone.now())
                    record_audit(
                        action="team_user_password_reset",
                        description=f"Password for {member_name} was reset from Settings.",
                        actor=request.user,
                        target=member,
                        metadata={"ip_address": client_ip(request)},
                    )
                    messages.success(request, f"New password saved for {member_name}. Share it with them privately.")
                    return redirect("settings_page")

    detected_time_zone = get_session_time_zone(request)
    saved_time_zone_preference = get_user_time_zone_preference(request.user)
    effective_time_zone = saved_time_zone_preference or detected_time_zone or getattr(
        request,
        "effective_time_zone",
        settings.TIME_ZONE,
    )
    raw_users = (
        user_model.objects.order_by("username")
        .annotate(
            sales_count=Count("sales_made", distinct=True),
            sales_value=Coalesce(Sum("sales_made__total_amount"), Decimal("0")),
        )
    )
    team_users = []
    for member in raw_users:
        role_value = get_user_role_value(member)
        role_label = get_user_role_label(member)
        team_users.append(
            {
                "user": member,
                "role_label": role_label,
                "role_value": role_value,
                "role_variant": "cashier" if role_value == "cashier" else ("custom" if role_value.startswith("custom:") else "admin"),
                "access_summary": get_user_role_summary(member),
                "is_protected": is_workspace_owner(member) or member.is_superuser,
                "sales_count": member.sales_count,
                "sales_value": member.sales_value,
            }
        )

    custom_roles = []
    for role in CustomRole.objects.order_by("name"):
        permission_items = []
        for module in MODULE_DEFINITIONS:
            level = (role.permissions or {}).get(module["key"], "none")
            if level == "none":
                continue
            permission_items.append(
                {
                    "label": module["label"],
                    "level": "Manage" if level == ACCESS_MANAGE else "View",
                    "tone": "healthy" if level == ACCESS_MANAGE else "clear",
                }
            )
        custom_roles.append(
            {
                "role": role,
                "permissions_summary": describe_permissions(role.permissions),
                "permission_items": permission_items,
                "member_count": user_model.objects.filter(groups__name=role.group_name).distinct().count(),
            }
        )

    session_logs = UserSessionLog.objects.select_related("user").order_by("-login_at")[:20]
    categories = Category.objects.order_by("name")
    bank_accounts = BankAccount.objects.select_related("account").order_by("name", "account_number")
    backup_snapshots = list(BackupSnapshot.objects.select_related("created_by").order_by("-created_at")[:10])
    for snapshot in backup_snapshots:
        snapshot.vendor = infer_backup_vendor(snapshot.file_name)
        snapshot.vendor_label = database_vendor_label(snapshot.vendor)
        snapshot.restore_block_reason = snapshot_restore_block_reason(snapshot)
        snapshot.can_restore_here = not snapshot.restore_block_reason

    workspace_billing_onboarding = workspace_owner_onboarding(request.user)
    billing_context = _billing_page_context(
        request,
        workspace_billing_onboarding,
        can_manage=user_can_manage_billing(request.user, workspace_billing_onboarding),
    )

    audit_log_queryset = AuditLog.objects.select_related("actor").order_by("-created_at", "-id")
    audit_event_count = audit_log_queryset.count()
    audit_mode = "all" if (request.GET.get("audit_mode") or "").lower() == "all" else "page"
    audit_page_obj = None
    if audit_mode == "all":
        audit_logs = list(audit_log_queryset)
    else:
        audit_page_obj = Paginator(audit_log_queryset, audit_page_size).get_page(request.GET.get("audit_page") or 1)
        audit_logs = list(audit_page_obj.object_list)

    context = {
        "page_title": "Settings",
        "page_subtitle": "Control branding, tax, POS rules, bank ledgers, backups, and staff access from one place.",
        "page_icon": "bi-sliders",
        "page_icon_accent": "slate",
        "team_form": team_form,
        "role_form": role_form,
        "branding_form": branding_form,
        "system_form": system_form,
        "time_zone_form": time_zone_form,
        "bank_form": bank_form,
        "role_choices": get_role_choices(),
        "team_users": team_users,
        "custom_roles": custom_roles,
        "session_logs": session_logs,
        "categories": categories,
        "bank_accounts": bank_accounts,
        "backup_runtime": backup_runtime,
        "backup_snapshots": backup_snapshots,
        "audit_logs": audit_logs,
        "audit_mode": audit_mode,
        "audit_page_obj": audit_page_obj,
        "audit_page_size": audit_page_size,
        "settings_obj": settings_obj,
        "total_team_users": len(team_users),
        "cashier_count": sum(1 for row in team_users if row["role_value"] == "cashier"),
        "custom_user_count": sum(1 for row in team_users if row["role_value"].startswith("custom:")),
        "custom_role_count": len(custom_roles),
        "active_sessions_count": UserSessionLog.objects.filter(logout_at__isnull=True).count(),
        "taxable_category_count": categories.filter(tax_status=Category.TAXABLE).count(),
        "gst_enabled_category_count": categories.exclude(tax_status=Category.EXEMPT).count(),
        "bank_ledger_count": bank_accounts.count(),
        "active_bank_count": bank_accounts.filter(is_active=True).count(),
        "backup_count": BackupSnapshot.objects.count(),
        "audit_event_count": audit_event_count,
        "effective_time_zone": effective_time_zone,
        "saved_time_zone_preference": saved_time_zone_preference,
        "session_detected_time_zone": detected_time_zone,
        "uses_system_detected_time_zone": not bool(saved_time_zone_preference),
        "lan_till_urls": _lan_till_urls(),
        "feature_groups": features_overview(),
        "edition_summary": edition_summary(),
        "team_module_locked": not module_within_ceiling("team"),
        **billing_context,
    }
    return render(request, "pos/settings.html", context)


@admin_required
@feature_required("backups")
def download_backup_snapshot(request, snapshot_id):
    snapshot = get_object_or_404(BackupSnapshot, pk=snapshot_id)
    try:
        backup_path = get_backup_file_path(snapshot)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("settings_page")

    record_audit(
        action="backup_downloaded",
        description=f"Backup snapshot '{snapshot.label}' was downloaded.",
        actor=request.user,
        target=snapshot,
        metadata={"file_name": snapshot.file_name},
    )
    return FileResponse(backup_path.open("rb"), as_attachment=True, filename=snapshot.file_name)
