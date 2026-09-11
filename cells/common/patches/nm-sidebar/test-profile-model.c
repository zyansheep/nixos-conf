#include "actions/profile-model.h"
#include <gio/gio.h>

static NMConnection *
profile(void)
{
  NMConnection *connection = nm_simple_connection_new();
  NMSetting *ip = nm_setting_ip4_config_new();
  NMIPRoute *route = nm_ip_route_new(AF_INET, "10.20.0.0", 16, "192.168.1.1", 30, NULL);
  g_object_set(ip, NM_SETTING_IP_CONFIG_METHOD, "auto", NM_SETTING_IP_CONFIG_ROUTE_METRIC, (gint64) 99, NULL);
  nm_setting_ip_config_add_route(NM_SETTING_IP_CONFIG(ip), route);
  nm_ip_route_unref(route);
  nm_connection_add_setting(connection, ip);
  NMSetting *security = nm_setting_wireless_security_new();
  g_object_set(security, NM_SETTING_WIRELESS_SECURITY_KEY_MGMT, "wpa-psk",
               NM_SETTING_WIRELESS_SECURITY_PSK, "test-only-password", NULL);
  nm_connection_add_setting(connection, security);
  return connection;
}

static void
manual_and_preservation(void)
{
  g_autoptr(NMConnection) original = profile();
  g_autoptr(NMConnection) draft = nm_simple_connection_new_clone(original);
  g_autoptr(GError) error = NULL;
  g_assert_true(sidebar_profile_set_ip(draft, AF_INET, "manual", "192.168.1.20/24, 192.168.1.21/24", "192.168.1.1", "1.1.1.1, 9.9.9.9", FALSE, &error));
  g_assert_no_error(error);
  NMSettingIPConfig *ip = nm_connection_get_setting_ip4_config(draft);
  g_assert_cmpuint(nm_setting_ip_config_get_num_addresses(ip), ==, 2);
  g_assert_cmpuint(nm_setting_ip_config_get_num_dns(ip), ==, 2);
  g_assert_cmpuint(nm_setting_ip_config_get_num_routes(ip), ==, 1);
  g_assert_cmpint(nm_setting_ip_config_get_route_metric(ip), ==, 99);
  g_assert_cmpstr(nm_setting_wireless_security_get_psk(nm_connection_get_setting_wireless_security(draft)), ==, "test-only-password");
  g_assert_cmpstr(nm_setting_ip_config_get_method(nm_connection_get_setting_ip4_config(original)), ==, "auto");
  g_assert_cmpuint(nm_setting_ip_config_get_num_addresses(nm_connection_get_setting_ip4_config(original)), ==, 0);
}

static void
reject_invalid(void)
{
  const char *bad_addresses[] = { "192.168.1.20", "192.168.1.20/33", "192.168.1.20/-1", "192.168.1.20/24/4", "2001:db8::1/64", "", NULL };
  for (guint i = 0; bad_addresses[i]; i++) {
    g_autoptr(NMConnection) draft = profile();
    g_autoptr(GError) error = NULL;
    g_assert_false(sidebar_profile_set_ip(draft, AF_INET, "manual", bad_addresses[i], "", "", TRUE, &error));
    g_assert_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT);
    g_assert_cmpstr(nm_setting_ip_config_get_method(nm_connection_get_setting_ip4_config(draft)), ==, "auto");
  }
  g_autoptr(NMConnection) draft = profile();
  g_autoptr(GError) error = NULL;
  g_assert_false(sidebar_profile_set_ip(draft, AF_INET, "auto", "", "::1", "", TRUE, &error));
  g_clear_error(&error);
  g_assert_false(sidebar_profile_set_ip(draft, AF_INET, "auto", "", "", "not-a-dns-server", FALSE, &error));
  g_assert_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT);
}

static void
ipv6_and_disable(void)
{
  g_autoptr(NMConnection) draft = profile();
  g_autoptr(GError) error = NULL;
  g_assert_true(sidebar_profile_set_ip(draft, AF_INET6, "manual", "2001:db8::20/64", "2001:db8::1", "2606:4700:4700::1111", FALSE, &error));
  g_assert_no_error(error);
  NMSettingIPConfig *ip = nm_connection_get_setting_ip6_config(draft);
  g_assert_true(nm_setting_verify(NM_SETTING(ip), draft, &error));
  g_assert_no_error(error);
  g_assert_true(sidebar_profile_set_ip(draft, AF_INET6, "disabled", "2001:db8::20/64", "2001:db8::1", "2606:4700:4700::1111", TRUE, &error));
  ip = nm_connection_get_setting_ip6_config(draft);
  g_assert_cmpuint(nm_setting_ip_config_get_num_addresses(ip), ==, 0);
  g_assert_null(nm_setting_ip_config_get_gateway(ip));
  g_assert_cmpuint(nm_setting_ip_config_get_num_dns(ip), ==, 0);
  g_assert_true(nm_setting_verify(NM_SETTING(ip), draft, &error));
  g_assert_no_error(error);
}

int main(int argc, char **argv)
{
  g_test_init(&argc, &argv, NULL);
  g_test_add_func("/profile/manual-preserves-unedited-data", manual_and_preservation);
  g_test_add_func("/profile/rejects-invalid-without-mutating", reject_invalid);
  g_test_add_func("/profile/ipv6-and-disable", ipv6_and_disable);
  return g_test_run();
}
