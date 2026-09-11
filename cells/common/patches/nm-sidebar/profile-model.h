#pragma once
#include <NetworkManager.h>

/* Operates on an editor-owned clone, never on NMRemoteConnection. */
gboolean sidebar_profile_set_ip(NMConnection *draft, int family,
                               const char *method, const char *addresses,
                               const char *gateway, const char *dns,
                               gboolean automatic_dns, GError **error);
char *sidebar_profile_addresses(NMSettingIPConfig *setting);
char *sidebar_profile_dns(NMSettingIPConfig *setting);
