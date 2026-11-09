# Z3 Code's server on functor (the `t3` CLI from cells/common/packages/t3-cli.nix), reached over the tailnet by
# isomorph's desktop app and the phone. z3-autoupdate (bump + apply) keeps it on the newest release and restarts
# these units after a switch; a switch alone doesn't restart user units.
{pkgs, ...}: {
  home-manager.users.zyansheep.home.packages = [pkgs.t3-cli];

  z3-autoupdate = {
    bump = true;
    restartUnits = ["t3code.service" "t3code-tailnet6.service"];
  };

  systemd.user.services = {
    t3code = {
      description = "T3 Code server (tailnet)";
      unitConfig.ConditionUser = "zyansheep";
      after = ["network-online.target"];
      wantedBy = ["default.target"];
      # Agents the server starts inherit this PATH (the profile's gh, go and the like included).
      environment = {
        PATH = pkgs.lib.mkForce "%h/.nix-profile/bin:/etc/profiles/per-user/%u/bin:/run/current-system/sw/bin:/run/wrappers/bin";
        T3CODE_ANNOTATION_EVENTS_URL = "http://functor:7853/v1/interactions";
      };
      # Restarted by z3-autoupdate once no thread runs, never in the middle of a switch.
      restartIfChanged = false;
      serviceConfig = {
        ExecStart = "${pkgs.t3-cli}/bin/t3 serve --host 100.64.0.4 --port 3773 --no-browser %h";
        Restart = "always";
        RestartSec = 10;
      };
    };
    # Native environment links need functor's Tailscale IPv6 address over plain http.
    t3code-tailnet6 = {
      description = "T3 Code server on functor's Tailscale IPv6 address";
      unitConfig.ConditionUser = "zyansheep";
      after = ["network.target"];
      wantedBy = ["default.target"];
      restartIfChanged = false;
      serviceConfig = {
        ExecStart = "${pkgs.socat}/bin/socat TCP6-LISTEN:3773,bind=[fd7a:115c:a1e0::4],fork,reuseaddr TCP4:100.64.0.4:3773";
        Restart = "always";
        RestartSec = 5;
      };
    };
  };
}
