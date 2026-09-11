#pragma once
#include "actions/network_actions.h"

void network_sidebar_show_profile(NetworkSidebarActions *actions, NMClient *client,
                                 AdwNavigationView *view, NMRemoteConnection *profile,
                                 NMActiveConnection *active);
void network_sidebar_edit_profile(NetworkSidebarActions *actions, NMClient *client,
                                 AdwNavigationView *view, NMRemoteConnection *profile,
                                 const char *ssid, gboolean enterprise);
