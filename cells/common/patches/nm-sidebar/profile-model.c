#include "actions/profile-model.h"
#include <gio/gio.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>

static gboolean
invalid(GError **error, const char *message)
{
  g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT, message);
  return FALSE;
}

char *
sidebar_profile_addresses(NMSettingIPConfig *setting)
{
  GString *text = g_string_new(NULL);
  for (guint i = 0; setting && i < nm_setting_ip_config_get_num_addresses(setting); i++) {
    NMIPAddress *address = nm_setting_ip_config_get_address(setting, i);
    if (i) g_string_append(text, ", ");
    g_string_append_printf(text, "%s/%u", nm_ip_address_get_address(address), nm_ip_address_get_prefix(address));
  }
  return g_string_free(text, FALSE);
}

char *
sidebar_profile_dns(NMSettingIPConfig *setting)
{
  GString *text = g_string_new(NULL);
  for (guint i = 0; setting && i < nm_setting_ip_config_get_num_dns(setting); i++) {
    if (i) g_string_append(text, ", ");
    g_string_append(text, nm_setting_ip_config_get_dns(setting, i));
  }
  return g_string_free(text, FALSE);
}

gboolean
sidebar_profile_set_ip(NMConnection *draft, int family,
                       const char *method, const char *addresses,
                       const char *gateway, const char *dns,
                       gboolean automatic_dns, GError **error)
{
  NMSettingIPConfig *old = family == AF_INET ? nm_connection_get_setting_ip4_config(draft) : nm_connection_get_setting_ip6_config(draft);
  g_autoptr(NMSetting) copy = old ? nm_setting_duplicate(NM_SETTING(old)) :
    (family == AF_INET ? nm_setting_ip4_config_new() : nm_setting_ip6_config_new());
  NMSettingIPConfig *setting = NM_SETTING_IP_CONFIG(copy);
  g_auto(GStrv) address_parts = g_strsplit_set(addresses, ", \t\n", -1);
  g_auto(GStrv) dns_parts = g_strsplit_set(dns, ", \t\n", -1);
  gboolean disabled = g_str_equal(method, "disabled") || g_str_equal(method, "ignore");
  gboolean same_addresses;
  gboolean same_dns;
  g_autofree char *original_addresses = sidebar_profile_addresses(old);
  g_autofree char *original_dns = sidebar_profile_dns(old);

  /* Keep address attributes (labels, etc.) when the field was not edited. */
  same_addresses = g_str_equal(original_addresses, addresses);
  same_dns = g_str_equal(original_dns, dns);
  g_object_set(setting, NM_SETTING_IP_CONFIG_METHOD, method, NULL);
  if (!same_addresses || disabled) {
    nm_setting_ip_config_clear_addresses(setting);
    for (guint i = 0; !disabled && address_parts[i]; i++) {
      g_auto(GStrv) pair = NULL;
      g_autoptr(GInetAddress) ip = NULL;
      NMIPAddress *address = NULL;
      char *end = NULL;
      guint64 prefix;
      if (!*address_parts[i]) continue;
      pair = g_strsplit(address_parts[i], "/", 3);
      if (!pair[1] || pair[2]) return invalid(error, "Use addresses with a prefix, such as 192.168.1.20/24 or 2001:db8::20/64.");
      ip = g_inet_address_new_from_string(pair[0]);
      errno = 0;
      prefix = g_ascii_strtoull(pair[1], &end, 10);
      if (!ip || (int) g_inet_address_get_family(ip) != family || errno || !*pair[1] || *end ||
          prefix == 0 || prefix > (family == AF_INET ? 32 : 128))
        return invalid(error, "An IP address or prefix is invalid for this address family.");
      address = nm_ip_address_new(family, pair[0], prefix, error);
      if (!address) return FALSE;
      nm_setting_ip_config_add_address(setting, address);
      nm_ip_address_unref(address);
    }
  }
  if (g_str_equal(method, "manual") && nm_setting_ip_config_get_num_addresses(setting) == 0)
    return invalid(error, "Manual configuration needs at least one IP address and prefix.");
  if (!disabled && *gateway) {
    g_autoptr(GInetAddress) ip = g_inet_address_new_from_string(gateway);
    if (!ip || (int) g_inet_address_get_family(ip) != family)
      return invalid(error, "The gateway is invalid for this address family.");
  }
  g_object_set(setting, NM_SETTING_IP_CONFIG_GATEWAY, !disabled && *gateway ? gateway : NULL,
               NM_SETTING_IP_CONFIG_IGNORE_AUTO_DNS, !automatic_dns, NULL);
  if (!same_dns || disabled) {
    nm_setting_ip_config_clear_dns(setting);
    for (guint i = 0; !disabled && dns_parts[i]; i++) {
      g_autoptr(GInetAddress) ip = NULL;
      if (!*dns_parts[i]) continue;
      ip = g_inet_address_new_from_string(dns_parts[i]);
      if (!ip || (int) g_inet_address_get_family(ip) != family)
        return invalid(error, "Enter DNS server IP addresses separated by commas, using the matching address family.");
      nm_setting_ip_config_add_dns(setting, dns_parts[i]);
    }
  }
  if (disabled) {
    nm_setting_ip_config_clear_routes(setting);
    nm_setting_ip_config_clear_dns_searches(setting);
  }
  nm_connection_add_setting(draft, g_steal_pointer(&copy));
  return TRUE;
}
