"""Host bindings for trusted adapters, owned by the existing Activation."""

from traceh.api.sandbox import SandboxOwner


class PluginSandboxFactory:
    def __init__(self, service, grants, data_dir):
        self._service = service
        self._grants = {grant.plugin_id: grant for grant in grants}
        self._data_dir = data_dir

    def bind(self, identity, activation):
        if identity.plugin_id != activation.plugin_id:
            raise ValueError("sandbox-plugin-activation-owner-mismatch")
        grant = self._grants.get(identity.plugin_id)
        if grant is None or grant.version != identity.version:
            return None
        return _ActivationProcesses(self._service, grant, activation, self._data_dir)


class _ActivationProcesses:
    def __init__(self, service, grant, activation, data_dir):
        self._service = service
        self._grant = grant
        self._activation = activation
        self._data_dir = data_dir
        self._launched = 0

    async def open(self, argv, *, timeout_seconds, cwd):
        if self._activation.disposed or self._activation.published:
            raise RuntimeError("sandbox-plugin-setup-closed")
        if self._launched >= self._grant.max_processes:
            raise ValueError("sandbox-plugin-process-limit")
        # Count attempted launches before the first await. Failed starts do not
        # create an unbounded implicit retry/restart budget.
        self._launched += 1
        activation_id = self._activation.activation_id
        owner = SandboxOwner(
            "plugin", activation_id, plugin_id=self._grant.plugin_id,
            plugin_version=self._grant.version, activation_id=activation_id,
        )
        scope = self._service.scope(
            owner, stream_id="plugin-activation:" + activation_id,
            workspace=self._grant.workspace, data_dir=self._data_dir, publish_changes=False,
        )

        async def close():
            await scope.__aexit__(None, None, None)

        # Own cleanup before any external effect or cancellable startup occurs.
        self._activation.add_cleanup(close)
        await scope.__aenter__()
        return await scope.open_process(
            argv, timeout_seconds=timeout_seconds, cwd=cwd, stdio=self._grant.stdio,
        )
