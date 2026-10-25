"""
PyBrowser Incognito & Private Browsing Module.
Manages non-persistent WebKit data stores and incognito session isolation.
"""

import WebKit
from pybrowser.settings import LOCAL_PROXY_PORT, SETTINGS
from pybrowser.doh_server import create_nw_proxy_config

def create_incognito_data_store():
    """Returns an isolated, non-persistent WKWebsiteDataStore with DoH proxy bound."""
    ds = WebKit.WKWebsiteDataStore.nonPersistentDataStore()
    pcfg = create_nw_proxy_config("127.0.0.1", LOCAL_PROXY_PORT)
    if pcfg and hasattr(ds, "setProxyConfigurations_"):
        try:
            ds.setProxyConfigurations_([pcfg])
        except Exception:
            pass
    return ds

def apply_incognito_preferences(config, shared_data_store=None):
    """Configures a WKWebViewConfiguration for zero-persistence incognito browsing."""
    data_store = shared_data_store or create_incognito_data_store()
    config.setWebsiteDataStore_(data_store)
    try:
        config.preferences().setValue_forKey_(True, "developerExtrasEnabled")
        config.preferences().setValue_forKey_(True, "fullScreenEnabled")
        if hasattr(config.preferences(), "setJavaScriptCanOpenWindowsAutomatically_"):
            config.preferences().setJavaScriptCanOpenWindowsAutomatically_(not SETTINGS.settings.get("block_popups", True))
        if hasattr(config.preferences(), "setJavaScriptEnabled_"):
            config.preferences().setJavaScriptEnabled_(SETTINGS.settings.get("js_enabled", True))
        if hasattr(config.preferences(), "setElementFullscreenEnabled_"):
            config.preferences().setElementFullscreenEnabled_(True)
    except Exception:
        pass
    return config
