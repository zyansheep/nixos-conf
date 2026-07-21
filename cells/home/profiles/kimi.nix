_: {pkgs, ...}: {
  # `kimi` — Moonshot's kimi-code agentic CLI, wrapped in a bubblewrap sandbox.
  #
  # The wrapper confines kimi (and every shell command the agent spawns) to the
  # current working directory, /tmp, and kimi's own state dir (~/.kimi-code).
  # The rest of $HOME (~/.ssh, ~/.aws, browser data, other projects) is
  # invisible; the system is read-only; the host env is cleared so shell
  # secrets don't leak into the agent. See ./_files/kimi-sandbox.sh for detail.
  #
  # The binary itself is NOT packaged here — it's the vendor build installed
  # imperatively into ~/.kimi-code/bin/kimi, which self-updates via
  # `kimi upgrade` (works from inside the sandbox: that dir is bound rw + net is
  # on). It runs natively via nix-ld, so no patchelf is needed. Install once:
  #   curl -fsSL https://code.kimi.com/kimi-code/install.sh | KIMI_NO_MODIFY_PATH=1 bash
  #
  # This wrapper is named `kimi` so plain `kimi` is always sandboxed; run
  # ~/.kimi-code/bin/kimi directly for an unsandboxed instance.
  home.packages = [
    (pkgs.writeShellApplication {
      name = "kimi";
      runtimeInputs = with pkgs; [bubblewrap coreutils];
      text = builtins.readFile ./_files/kimi-sandbox.sh;
    })
  ];
}
