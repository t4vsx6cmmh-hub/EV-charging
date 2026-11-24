"""Tesla Fleet API: læs bilens batteriniveau og start/stop opladning.

Kræver en (gratis) udviklerkonto på developer.tesla.com og et login én gang:

    python3 -m elopladning --tesla-login

Nyere biler kræver signerede kommandoer. Kør da Teslas `tesla-http-proxy`
(github.com/teslamotors/vehicle-command) lokalt og sæt `tesla_command_proxy`
til fx "https://localhost:4443" og `tesla_proxy_ca_file` til proxyens certifikat.
Data (batteri) hentes direkte fra Fleet API; kun kommandoer går via proxyen.
"""
import secrets
import ssl
import time
import urllib.parse

from .net import HTTPError, request_json

AUTHORIZE_URL = "https://auth.tesla.com/oauth2/v3/authorize"
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
REGIONS = {
    "eu": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
    "na": "https://fleet-api.prd.na.vn.cloud.tesla.com",
}
SCOPES = "openid offline_access vehicle_device_data vehicle_charging_cmds"
# Svar fra bilen der betyder "allerede i den ønskede tilstand"
OK_REASONS = {"is_charging", "not_charging", "complete", "requested"}


class TeslaError(RuntimeError):
    pass


class VehicleAsleep(TeslaError):
    pass


def saved_tokens(store):
    return store.get_kv("tesla_tokens") or {}


def configured(cfg, store):
    return bool(cfg["tesla_client_id"]) and bool(
        cfg["tesla_refresh_token"].strip() or saved_tokens(store).get("refresh"))


def from_config(cfg, store):
    return TeslaFleet(cfg["tesla_client_id"], cfg["tesla_refresh_token"], store,
                      vin=cfg["tesla_vin"],
                      api_base=cfg["tesla_api_base"] or REGIONS[cfg["tesla_region"]],
                      command_base=cfg["tesla_command_proxy"],
                      ca_file=cfg["tesla_proxy_ca_file"])


class TeslaFleet:
    def __init__(self, client_id, refresh_token, store, vin="", api_base=REGIONS["eu"],
                 command_base="", ca_file="", token_url=TOKEN_URL):
        self.client_id = client_id
        self.store = store
        self.vin = vin
        self.api_base = api_base.rstrip("/")
        self.command_base = (command_base or api_base).rstrip("/")
        self.command_ctx = ssl.create_default_context(cafile=ca_file) if ca_file else None
        self.token_url = token_url
        self._access = None
        self._expires = 0.0
        # Tesla udsteder et nyt refresh token ved hver fornyelse. Det nyeste gemmes
        # i databasen; et nyt token i config.json (seed) har dog forrang.
        cfg_token = (refresh_token or "").strip()
        saved = saved_tokens(store)
        if saved.get("refresh") and (not cfg_token or saved.get("seed") == cfg_token):
            self._refresh, self._seed = saved["refresh"], saved.get("seed", "")
        else:
            self._refresh, self._seed = cfg_token, cfg_token
        if not self._refresh:
            raise TeslaError("Mangler Tesla-login – kør: python3 -m elopladning --tesla-login")

    # --------------------------------------------------------------- tokens --
    def _save(self, tokens):
        self._access = tokens["access_token"]
        self._expires = time.time() + float(tokens.get("expires_in", 8 * 3600))
        if tokens.get("refresh_token"):
            self._refresh = tokens["refresh_token"]
        self.store.set_kv("tesla_tokens", {"seed": self._seed, "refresh": self._refresh})

    def access_token(self):
        if self._access and time.time() < self._expires - 300:
            return self._access
        try:
            tokens = request_json(self.token_url, "POST", retries=1, form={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": self._refresh,
            })
        except HTTPError as e:
            if e.status in (400, 401, 403):
                raise TeslaError("Tesla afviste login – kør --tesla-login igen") from None
            raise
        self._save(tokens)
        return self._access

    # ------------------------------------------------------------- kald --
    def _call(self, method, path, command=False, body=None):
        base = self.command_base if command else self.api_base
        ctx = self.command_ctx if command else None
        for attempt in (0, 1):
            try:
                return request_json(base + path, method, body=body, timeout=30, retries=1,
                                    context=ctx,
                                    headers={"Authorization": f"Bearer {self.access_token()}"})
            except HTTPError as e:
                if e.status == 401 and attempt == 0:
                    self._access = None  # udløbet – forny og prøv igen
                    continue
                if e.status == 408:
                    raise VehicleAsleep("bilen sover") from None
                raise

    def vehicle(self):
        if not self.vin:
            cars = self._call("GET", "/api/1/vehicles").get("response") or []
            if not cars:
                raise TeslaError("Ingen biler fundet på Tesla-kontoen")
            self.vin = cars[0]["vin"]
        return self.vin

    def charge_state(self):
        """{'battery_level', 'charge_limit_soc', 'charging_state', ...}"""
        r = self._call("GET", f"/api/1/vehicles/{self.vehicle()}/vehicle_data"
                              "?endpoints=charge_state")
        cs = (r.get("response") or {}).get("charge_state")
        if not cs:
            raise TeslaError("Tesla sendte ingen ladestatus")
        return cs

    def wake(self):
        self._call("POST", f"/api/1/vehicles/{self.vehicle()}/wake_up")

    def command(self, name):
        r = self._call("POST", f"/api/1/vehicles/{self.vehicle()}/command/{name}",
                       command=True, body={})
        resp = r.get("response") or {}
        reason = resp.get("reason") or ""
        if not resp.get("result") and reason not in OK_REASONS:
            raise TeslaError(f"{name} afvist af bilen: {reason or 'ukendt årsag'}")
        return reason

    def charge_start(self):
        return self.command("charge_start")

    def charge_stop(self):
        return self.command("charge_stop")


# ------------------------------------------------------------------ login --

def authorize_url(client_id, redirect_uri, state):
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
    })


def exchange_code(client_id, client_secret, code, redirect_uri, audience,
                  token_url=TOKEN_URL):
    return request_json(token_url, "POST", retries=1, form={
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "audience": audience,
    })


def code_from_redirect(url_or_code, state=None):
    """Find 'code' i adressen, Tesla sendte dig videre til (eller selve koden)."""
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url_or_code.strip()).query)
    if q.get("error"):
        raise TeslaError(f"Tesla afviste login: {q['error'][0]}")
    if state and q.get("state") and q["state"][0] != state:
        raise TeslaError("Forkert 'state' i adressen – start login forfra.")
    code = q.get("code", [url_or_code.strip()])[0]
    if not code or "://" in code:
        raise TeslaError("Fandt ingen kode i adressen.")
    return code


def finish_login(cfg, store, code, token_url=TOKEN_URL):
    """Byt koden til tokens og gem dem i databasen."""
    audience = cfg["tesla_api_base"] or REGIONS[cfg["tesla_region"]]
    tokens = exchange_code(cfg["tesla_client_id"], cfg["tesla_client_secret"], code,
                           cfg["tesla_redirect_uri"], audience, token_url=token_url)
    if not tokens.get("refresh_token"):
        raise TeslaError("Tesla sendte intet refresh token (mangler scope offline_access?)")
    store.set_kv("tesla_tokens", {"seed": cfg["tesla_refresh_token"].strip(),
                                  "refresh": tokens["refresh_token"]})
    return tokens


def login_cli(cfg, store, input_fn=input, print_fn=print, token_url=TOKEN_URL):
    """Interaktivt login: åbn adressen, log ind hos Tesla, indsæt adressen du lander på."""
    missing = [k for k in ("tesla_client_id", "tesla_client_secret", "tesla_redirect_uri")
               if not cfg[k]]
    if missing:
        raise SystemExit("Udfyld først i config.json: " + ", ".join(missing))
    state = secrets.token_urlsafe(16)
    print_fn("1. Åbn denne adresse i en browser og log ind med din Tesla-konto:\n")
    print_fn("   " + authorize_url(cfg["tesla_client_id"], cfg["tesla_redirect_uri"], state) + "\n")
    print_fn("2. Når du er sendt videre (siden må gerne vise en fejl), så kopier hele adressen.")
    pasted = input_fn("\nIndsæt adressen her: ").strip()
    try:
        code = code_from_redirect(pasted, state)
        tokens = finish_login(cfg, store, code, token_url=token_url)
    except TeslaError as e:
        raise SystemExit(str(e)) from None
    print_fn("\nLogin gemt i databasen.")
    try:
        car = from_config(cfg, store)
        car.token_url = token_url
        car._save(tokens)
        print_fn(f"Fandt bil: {car.vehicle()}")
    except Exception as e:  # login er gemt; bilopslag er kun en kontrol
        print_fn(f"(Kunne ikke slå bilen op endnu: {e})")
