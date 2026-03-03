import os
import streamlit as st
import snowflake.connector
from dotenv import load_dotenv

load_dotenv()


def _create_connection():
    """Create a new Snowflake connection.

    Supports optional SNOWFLAKE_AUTHENTICATOR env var.
    Set to 'externalbrowser' for local development with SSO.
    Leave unset (default 'snowflake') for service account with password.
    """
    authenticator = os.environ.get("SNOWFLAKE_AUTHENTICATOR", "snowflake")
    connect_kwargs = dict(
        user=os.environ["SNOWFLAKE_USER"],
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        role=os.environ.get("SNOWFLAKE_ROLE", "PUBLIC"),
        database=os.environ.get("SNOWFLAKE_DATABASE", ""),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", ""),
        authenticator=authenticator,
        login_timeout=60,           # timeout para autenticar (mas tiempo para MFA)
        network_timeout=90,         # 90s max por query — evita colgarse para siempre.
                                    # Chile no lo necesita (tablas rapidas), Peru si.
                                    # Queries que pasen 90s fallaran con error visible
                                    # en el expander del dashboard, no colgan la app.
        # MFA token caching: la 1ra vez pide el MFA, despues usa el token
        # guardado localmente sin volver a pedir el segundo factor.
        client_store_temporary_credential=True,
    )
    if authenticator == "snowflake":
        connect_kwargs["password"] = os.environ["SNOWFLAKE_PASSWORD"]
    return snowflake.connector.connect(**connect_kwargs)


@st.cache_resource
def get_snowflake_connection():
    try:
        return _create_connection()
    except Exception as e:
        st.error(f"Error conectando a Snowflake: {e}")
        st.stop()


def get_active_connection():
    """Return an active Snowflake connection, reconnecting if the token has expired."""
    conn = get_snowflake_connection()
    try:
        # Lightweight ping to verify the connection is still alive
        conn.cursor().execute("SELECT 1")
        return conn
    except Exception:
        # Token expired or connection dropped — clear cache and reconnect
        get_snowflake_connection.clear()
        try:
            new_conn = _create_connection()
            # Store the fresh connection back in cache
            get_snowflake_connection.__wrapped__ = lambda: new_conn  # noqa
            return new_conn
        except Exception as e:
            st.error(f"Error reconectando a Snowflake: {e}")
            st.stop()
