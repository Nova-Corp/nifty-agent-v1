"""Option contract lookup: expiry selection and strike selection."""
from __future__ import annotations
import logging
from abc import ABC, abstractmethod
from datetime import date
from typing import Optional
from .models import OptionContract

log = logging.getLogger("instruments")


def atm_strike(spot: float, step: int) -> int:
    return int(round(spot / step) * step)


def pick_strike(spot: float, step: int, opt_type: str, itm_offset: int = 0) -> int:
    """itm_offset=0 -> ATM; 1 -> one strike in-the-money (lower for CE, higher for PE)."""
    atm = atm_strike(spot, step)
    return atm - itm_offset * step if opt_type == "CE" else atm + itm_offset * step


class InstrumentProvider(ABC):
    @abstractmethod
    def expiries(self) -> list[date]: ...
    @abstractmethod
    def option(self, strike: float, opt_type: str, expiry: date) -> Optional[OptionContract]: ...

    def select_expiry(self, today: date, min_dte: int) -> date:
        """Nearest expiry at least min_dte calendar days away (min_dte=1 avoids 0-DTE)."""
        for e in sorted(self.expiries()):
            if (e - today).days >= min_dte:
                return e
        raise RuntimeError("no suitable expiry found in contract master")


class AliceInstruments(InstrumentProvider):
    """Reads Alice Blue's NFO contract master (downloaded by pya3 as NFO.csv).

    Column names are matched loosely; run check_setup.py once to confirm they parse.
    """

    CANDIDATES = {
        "symbol": ["symbol"],
        "token": ["token"],
        "opt": ["option type", "optiontype", "option_type"],
        "strike": ["strike price", "strikeprice", "strike_price", "strike"],
        "expiry": ["expiry date", "expirydate", "expiry_date", "expiry"],
        "lot": ["lot size", "lotsize", "lot_size"],
        "tsym": ["trading symbol", "tradingsymbol", "trading_symbol"],
        "itype": ["instrument type", "instrumenttype", "instrument_type"],
    }

    def __init__(self, alice, underlying: str = "NIFTY", exchange: str = "NFO", master_path: Optional[str] = None,
                 lot_size_fallback: int = 65):
        import pandas as pd
        self.exchange, self.underlying = exchange, underlying
        if master_path is None:
            alice.get_contract_master(exchange)
            master_path = f"{exchange}.csv"
        df = pd.read_csv(master_path, low_memory=False)
        cols = {c.lower().strip(): c for c in df.columns}

        def col(key, required=True):
            for cand in self.CANDIDATES[key]:
                if cand in cols:
                    return cols[cand]
            if required:
                raise RuntimeError(f"contract master: no column for {key}; columns are {list(df.columns)}")
            return None

        c_sym, c_tok, c_opt, c_str, c_exp = col("symbol"), col("token"), col("opt"), col("strike"), col("expiry")
        c_lot, c_tsym, c_it = col("lot", False), col("tsym", False), col("itype", False)
        df = df[(df[c_sym].astype(str).str.upper() == underlying) & (df[c_opt].astype(str).str.upper().isin(["CE", "PE"]))]
        if c_it:
            idx = df[c_it].astype(str).str.upper().str.contains("OPTIDX")
            if idx.any():
                df = df[idx]
        exp = df[c_exp]
        if pd.api.types.is_numeric_dtype(exp):
            unit = "ms" if exp.max() > 1e11 else "s"
            exp = pd.to_datetime(exp, unit=unit, utc=True).dt.tz_convert("Asia/Kolkata")
        else:
            exp = pd.to_datetime(exp, errors="coerce")
        self.rows = []
        for (_, r), e in zip(df.iterrows(), exp):
            if pd.isna(e):
                continue
            lot = int(r[c_lot]) if c_lot and not pd.isna(r[c_lot]) else lot_size_fallback
            self.rows.append(OptionContract(exchange=exchange, token=str(int(r[c_tok])),
                                            symbol=str(r[c_tsym]) if c_tsym else f"{underlying}{e.date()}{r[c_str]}{r[c_opt]}",
                                            underlying=underlying, strike=float(r[c_str]),
                                            option_type=str(r[c_opt]).upper(), expiry=e.date(), lot_size=lot))
        if not self.rows:
            raise RuntimeError(f"no {underlying} options parsed from contract master")
        lots = {c.lot_size for c in self.rows}
        if lots != {lot_size_fallback}:
            log.warning("lot size in master %s differs from config %s; using master", lots, lot_size_fallback)
        self._index = {(c.strike, c.option_type, c.expiry): c for c in self.rows}
        log.info("loaded %d %s option contracts, expiries: %s", len(self.rows), underlying, self.expiries()[:4])

    def expiries(self) -> list[date]:
        return sorted({c.expiry for c in self.rows})

    def option(self, strike, opt_type, expiry):
        return self._index.get((float(strike), opt_type, expiry))
