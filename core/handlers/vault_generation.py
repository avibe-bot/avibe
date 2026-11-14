"""Service-owned static generation: authenticated admission, custody, atomic commit."""
from __future__ import annotations

from argparse import Namespace
from typing import Any

from core.control_requests import ControlRequest, failure, result

HELP = "vibe vault generate --help"
_METADATA = ("description", "tag", "skill", "allow_host", "fetch_auth", "auth_name")


def _arguments(arguments: dict[str, Any]) -> Namespace:
    allowed = {"name", "field", "recipe", "protection", *_METADATA}
    if arguments.keys() - allowed:
        raise ValueError("Unknown Vault generation arguments")
    values = {key: arguments.get(key) for key in allowed}
    if not isinstance(values["name"], str) or not isinstance(values["recipe"], dict):
        raise ValueError("Invalid Vault generation arguments")
    for key in ("field", "protection", "description", "fetch_auth", "auth_name"):
        if values[key] is not None and not isinstance(values[key], str):
            raise ValueError("Invalid Vault generation arguments")
    for key in ("tag", "skill", "allow_host"):
        if values[key] is not None and (not isinstance(values[key], list) or any(not isinstance(item, str) for item in values[key])):
            raise ValueError("Invalid Vault generation metadata")
    return Namespace(**values)


def _generate(request: ControlRequest) -> dict[str, Any]:
    """One worker owns preflight through commit, including disconnected callers."""
    from core.caller_context import caller_context_from_env
    from storage import vault_generation as generation, vault_service as vs
    from storage.db import get_cached_sqlite_engine
    from vibe import api, cli
    from vibe.authorization import invocation_authority

    committed = False
    commit_ready = False
    try:
        args = _arguments(request.arguments)
        authority = cli._cli_invocation_authority(request.caller_env)
        with invocation_authority(authority):
            cli._require_command_path_admission(("vault", "generate"))
            if request.expired:
                return failure("deadline_expired", "Control request expired before execution")
            if args.protection not in (None, "standard"):
                raise generation.GenerateTargetError("generation supports only Standard secrets; use vibe vault request for Protected secrets")
            try:
                if "field" in args.recipe:
                    raise vs.InvalidRequestError("field belongs to the generation target, not the recipe")
                recipe = vs.normalize_generate_recipe(args.recipe)
            except vs.VaultServiceError as exc:
                raise cli.TaskCliError(str(exc), code="invalid_generate_recipe", help_command=HELP) from exc
            shortest, longest = api.AVAULT_GENERATE_PASSWORD_MIN_LENGTH, vs.GENERATE_PASSWORD_LENGTH[1]
            if recipe["type"] == "password" and recipe["length"] < shortest:
                raise cli.TaskCliError(
                    f"an Agent-generated password is at least {shortest} characters ({shortest} to {longest})",
                    code="invalid_generate_recipe", help_command=HELP,
                    hint="For a shorter password, use vibe vault request with a generate preset of that length; the user generates it in the browser.",
                )
            engine = get_cached_sqlite_engine()
            with engine.connect() as conn:
                snapshot = generation.capture_target(conn, args.name, args.field)
                current = vs._meta_payload(snapshot["row"]) if snapshot["row"] else {}
            metadata = {}
            if any(getattr(args, key) is not None for key in _METADATA):
                metadata = generation.normalize_metadata(cli._vault_edit_payload_from_args(args, current=current, help_command=HELP))
            caller = caller_context_from_env(request.caller_env)
            session_id = cli._default_session_id_from_caller(caller)
            if request.expired:
                return failure("deadline_expired", "Control request expired before execution")
            sealed = api.avault_generate_secret(snapshot["record"], recipe)
            release_scopes = []
            with engine.begin() as conn:
                payload = generation.commit_generated(conn, snapshot, sealed, recipe=recipe, metadata=metadata,
                                                      session_id=session_id, release_scopes=release_scopes)
                commit_ready = True
            committed = True
            payload["secret"] = cli._vault_secret_row(payload["secret"])
            # Committed state is authoritative even if a best-effort notification
            # fails. Never turn a successful generation into permission to replay.
            try:
                api.release_vault_agent_scopes(release_scopes, reason="vault_generate")
                cli._publish_cli_vaults_updated(scope="secret", secret_name=args.name)
            except Exception:
                pass
            return result(payload)
    except Exception as exc:
        if committed or commit_ready:
            return failure("execution_unknown", "Generation may have committed; inspect vibe vault list before retrying", execution="execution_unknown")
        if isinstance(exc, generation.GenerateTargetError):
            error = cli.TaskCliError(str(exc), code="generate_target_not_allowed", help_command=HELP,
                                     hint="Use vibe vault request for an existing value or a Protected secret.")
        elif isinstance(exc, api.AvaultError):
            if getattr(exc, "minimum_version", None) == api.AVAULT_GENERATE_SECRET_MIN_VERSION:
                minimum = api.AVAULT_GENERATE_SECRET_MIN_VERSION
                error = cli.TaskCliError(
                    f"static secret generation requires avault >= {minimum}", code="avault_upgrade_required",
                    details={"minimum_version": minimum}, help_command=HELP,
                    hint="Wait for the approved avault update; do not install an arbitrary binary or generate outside custody.",
                )
            else:
                error = cli._avault_cli_error(exc, "avault could not generate and seal the secret", help_command=HELP)
        elif isinstance(exc, (vs.SecretExistsError, vs.SecretNameCaseConflictError)):
            error = cli.TaskCliError(str(exc), code="generate_target_not_allowed", help_command=HELP,
                                     hint="Use vibe vault request for the existing secret; generation cannot overwrite it.")
        elif isinstance(exc, vs.SecretConflictError):
            error = cli.TaskCliError(str(exc), code="secret_conflict", help_command=HELP)
        elif isinstance(exc, vs.VaultServiceError):
            error = cli.TaskCliError(str(exc), code="invalid_generate_target", help_command=HELP)
        elif isinstance(exc, cli.TaskCliError):
            error = exc
        elif isinstance(exc, (ValueError, TypeError)):
            error = cli.TaskCliError("Invalid Vault generation arguments", code="invalid_generate_arguments", help_command=HELP)
        else:
            # No raw exception, traceback, custody stdout/stderr or recipe echo.
            error = cli.TaskCliError("Vault generation failed before storage", code="generation_failed", help_command=HELP)
        return result(cli._task_error_payload(error, help_command=HELP), execution="not_executed")


async def handle_generate_control_request(request: ControlRequest, *, run_sync) -> dict[str, Any]:
    from core.runtime_work import land_step

    landed, cancelled = await land_step(run_sync(lambda: _generate(request)))
    response = landed.result()
    if cancelled:
        import asyncio

        raise asyncio.CancelledError
    return response
