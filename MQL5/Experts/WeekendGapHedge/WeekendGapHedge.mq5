//+------------------------------------------------------------------+
//|                                             WeekendGapHedge.mq5  |
//|                                                                  |
//|  Research EA for the weekend gap trade.                           |
//|                                                                  |
//|  Opens a max-size position in the final seconds of the trading    |
//|  week and harvests the Monday open gap.                           |
//|                                                                  |
//|  Two deployment shapes:                                           |
//|                                                                  |
//|    LEG_BOTH  - buy and sell on one hedging account. This is the   |
//|                probe. A stop loss cannot cap the losing leg       |
//|                across a gap, so expect the overshoot to show up   |
//|                as a real cost. The journal quantifies it.         |
//|                                                                  |
//|    LEG_LONG  - one side only, one account, one broker. Run the    |
//|    LEG_SHORT   opposite side on a second account at a second      |
//|                broker. Each account's downside is then capped by  |
//|                its own stop-out and negative balance protection,  |
//|                which is a real cap rather than an order type the  |
//|                market can gap through.                            |
//|                                                                  |
//|  Every cycle is written to Files\WGH_cycles_<symbol>.csv with     |
//|  both the realised P&L and the counterfactual P&L that would      |
//|  have resulted had each leg's loss been capped at the amount      |
//|  originally risked.                                               |
//+------------------------------------------------------------------+
#property copyright "Gap-Trading"
#property version   "1.10"
#property description "Weekend gap trade: max-size entry at the weekly close, gap harvest at the reopen."

#include <Trade\Trade.mqh>

//+------------------------------------------------------------------+
//| Enumerations                                                     |
//+------------------------------------------------------------------+
enum ENUM_LEG_MODE
  {
   LEG_BOTH  = 0,   // Both legs on this account (needs a hedging account)
   LEG_LONG  = 1,   // Long only - pair with a short account at another broker
   LEG_SHORT = 2    // Short only - pair with a long account at another broker
  };

enum ENUM_SIZING_MODE
  {
   SIZING_MAX_MARGIN = 0,  // Max lots affordable on the allocated free margin
   SIZING_FIXED_RISK = 1   // Lots sized so the stop costs a fixed % of equity
  };

enum ENUM_LOSER_MODE
  {
   LOSER_STOP_LOSS  = 0,   // Stop loss takes the losing leg out (gaps through it)
   LOSER_CLOSE_OPEN = 1,   // Market-close the losing leg on the first tick
   LOSER_HOLD_BOTH  = 2    // Symmetric baseline - hold both, exit together
  };

enum ENUM_EXIT_MODE
  {
   EXIT_TIMED      = 0,    // (a) Close N minutes after the market reopens
   EXIT_R_MULTIPLE = 1,    // (b) Take profit at a multiple of the amount risked
   EXIT_HYBRID     = 2     // (c) R-multiple target with a timed backstop
  };

//+------------------------------------------------------------------+
//| Inputs                                                           |
//+------------------------------------------------------------------+
input group "=== Deployment ==="
input ENUM_LEG_MODE    InpLegMode          = LEG_BOTH;
input ulong            InpMagic            = 20260803;

input group "=== Entry ==="
input int              InpEntrySeconds     = 30;      // Seconds before weekly close to enter
input int              InpEntryDeadline    = 3;       // Stop trying with this many seconds left
input int              InpMaxSpreadPoints  = 0;       // Skip entry above this spread (0 = no limit)

input group "=== Position sizing ==="
input ENUM_SIZING_MODE InpSizingMode       = SIZING_MAX_MARGIN;
input double           InpMarginBufferPct  = 80.0;    // % of allocated free margin to commit
input double           InpRiskPercent      = 2.0;     // Equity % at risk per leg (SIZING_FIXED_RISK)
input double           InpLotCap           = 0.0;     // Hard lot ceiling per leg (0 = broker max)

input group "=== Stops ==="
input bool             InpUseStopLoss      = true;    // Attach an SL (cannot survive a gap - see journal)
input double           InpStopLossPoints   = 0;       // SL distance in points (0 = auto from ATR)
input int              InpAtrPeriod        = 14;      // ATR period for the auto stop
input double           InpAtrMultiplier    = 1.5;     // Auto stop = ATR * this

input group "=== Exit ==="
input ENUM_LOSER_MODE  InpLoserMode        = LOSER_STOP_LOSS;
input ENUM_EXIT_MODE   InpExitMode         = EXIT_HYBRID;
input int              InpTimedExitMinutes = 60;      // Minutes after reopen to flatten
input double           InpTakeProfitR      = 2.0;     // TP as a multiple of the risked distance
input double           InpBreakEvenR       = 1.0;     // Move winner to breakeven at this R (0 = off)

input group "=== Execution ==="
input int              InpSlippagePoints   = 100;     // Deviation allowance in points
input bool             InpAllowMultiWeek   = true;    // Allow a new cycle each week
input bool             InpWriteJournal     = true;    // Write the CSV research journal
input bool             InpVerboseLog       = true;

//+------------------------------------------------------------------+
//| Cycle record - one weekend, written to CSV once both legs close  |
//+------------------------------------------------------------------+
struct CycleRecord
  {
   int      weekId;
   datetime entryTime;
   double   entryBid;
   double   entryAsk;
   double   volume;
   double   stopPoints;
   double   riskMoney;        // account currency at risk per leg, by design
   datetime reopenTime;
   double   reopenBid;
   double   reopenAsk;

   ulong    longPosId;
   ulong    shortPosId;
   bool     longOpen;
   bool     shortOpen;

   double   longExitPrice;
   double   longProfit;       // realised, incl. commission and swap
   string   longReason;
   double   shortExitPrice;
   double   shortProfit;
   string   shortReason;
  };

//+------------------------------------------------------------------+
//| Globals                                                          |
//+------------------------------------------------------------------+
CTrade      g_trade;
CycleRecord g_cycle;

int      g_atrHandle    = INVALID_HANDLE;
datetime g_lastTickTime = 0;   // previous tick's server time (gap detector)
datetime g_weekOpenTime = 0;   // first tick of the new week, 0 until detected
double   g_riskPoints   = 0;   // stop distance used at entry, in points
int      g_entryWeekId  = 0;   // yyyyww of the last cycle we opened

string   g_gvPrefix     = "";
string   g_cycleFile    = "";
string   g_dealFile     = "";

//--- how big a tick-time discontinuity counts as "the weekend"
#define WEEKEND_GAP_SECONDS 900

//+------------------------------------------------------------------+
//| Logging                                                          |
//+------------------------------------------------------------------+
void Log(const string msg)
  {
   if(InpVerboseLog)
      PrintFormat("[%s] %s", _Symbol, msg);
  }

//+------------------------------------------------------------------+
//| CSV journal                                                      |
//+------------------------------------------------------------------+
void CsvAppend(const string file, const string header, const string line)
  {
   if(!InpWriteJournal)
      return;

   bool isNew = !FileIsExist(file);
   int handle = FileOpen(file, FILE_READ | FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle == INVALID_HANDLE)
     {
      Log(StringFormat("Could not open journal %s (error %d)", file, GetLastError()));
      return;
     }

   FileSeek(handle, 0, SEEK_END);
   if(isNew)
      FileWriteString(handle, header + "\r\n");
   FileWriteString(handle, line + "\r\n");
   FileClose(handle);
  }

string CycleHeader()
  {
   return "week_id,symbol,leg_mode,sizing_mode,loser_mode,exit_mode,timed_minutes,tp_r,"
          "entry_time,entry_bid,entry_ask,volume,stop_points,risk_money_per_leg,"
          "reopen_time,reopen_bid,reopen_ask,gap_points,gap_pct,"
          "long_exit_price,long_profit,long_reason,"
          "short_exit_price,short_profit,short_reason,"
          "realised_net,capped_net,overshoot_cost,equity_after";
  }

string DealHeader()
  {
   return "time,symbol,deal_ticket,position_id,type,entry,volume,price,sl,tp,"
          "profit,commission,swap,reason,comment";
  }

//--- write the finished cycle, including the capped-loss counterfactual
void WriteCycle()
  {
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   int    digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);

   double entryMid  = (g_cycle.entryBid + g_cycle.entryAsk) / 2.0;
   double reopenMid = (g_cycle.reopenBid + g_cycle.reopenAsk) / 2.0;

   double gapPoints = 0.0, gapPct = 0.0;
   if(point > 0.0 && entryMid > 0.0 && reopenMid > 0.0)
     {
      gapPoints = (reopenMid - entryMid) / point;
      gapPct    = (reopenMid - entryMid) / entryMid * 100.0;
     }

   double realisedNet = g_cycle.longProfit + g_cycle.shortProfit;

   //--- the counterfactual: what each leg would have cost had its stop actually
   //--- held at the distance we risked, instead of being gapped through
   double risk = g_cycle.riskMoney;
   double cappedLong  = (risk > 0.0) ? MathMax(g_cycle.longProfit,  -risk) : g_cycle.longProfit;
   double cappedShort = (risk > 0.0) ? MathMax(g_cycle.shortProfit, -risk) : g_cycle.shortProfit;
   double cappedNet   = cappedLong + cappedShort;

   //--- the price of the gap jumping the stop; the number this EA exists to measure
   double overshoot = cappedNet - realisedNet;

   string line = StringFormat(
      "%d,%s,%s,%s,%s,%s,%d,%.2f,"
      "%s,%.*f,%.*f,%.2f,%.1f,%.2f,"
      "%s,%.*f,%.*f,%.1f,%.4f,"
      "%.*f,%.2f,%s,"
      "%.*f,%.2f,%s,"
      "%.2f,%.2f,%.2f,%.2f",
      g_cycle.weekId, _Symbol,
      EnumToString(InpLegMode), EnumToString(InpSizingMode),
      EnumToString(InpLoserMode), EnumToString(InpExitMode),
      InpTimedExitMinutes, InpTakeProfitR,
      TimeToString(g_cycle.entryTime, TIME_DATE | TIME_SECONDS),
      digits, g_cycle.entryBid, digits, g_cycle.entryAsk,
      g_cycle.volume, g_cycle.stopPoints, g_cycle.riskMoney,
      TimeToString(g_cycle.reopenTime, TIME_DATE | TIME_SECONDS),
      digits, g_cycle.reopenBid, digits, g_cycle.reopenAsk,
      gapPoints, gapPct,
      digits, g_cycle.longExitPrice, g_cycle.longProfit, g_cycle.longReason,
      digits, g_cycle.shortExitPrice, g_cycle.shortProfit, g_cycle.shortReason,
      realisedNet, cappedNet, overshoot,
      AccountInfoDouble(ACCOUNT_EQUITY));

   CsvAppend(g_cycleFile, CycleHeader(), line);

   Log(StringFormat("Cycle %d closed: gap %.1f pts, realised %.2f, capped-at-risk %.2f, overshoot cost %.2f",
                    g_cycle.weekId, gapPoints, realisedNet, cappedNet, overshoot));
  }

void ResetCycle()
  {
   ZeroMemory(g_cycle);
   g_cycle.longReason  = "";
   g_cycle.shortReason = "";
  }

//+------------------------------------------------------------------+
//| Persisted state                                                  |
//+------------------------------------------------------------------+
void StateSave()
  {
   GlobalVariableSet(g_gvPrefix + "lastTick",  (double)g_lastTickTime);
   GlobalVariableSet(g_gvPrefix + "weekOpen",  (double)g_weekOpenTime);
   GlobalVariableSet(g_gvPrefix + "riskPts",   g_riskPoints);
   GlobalVariableSet(g_gvPrefix + "weekId",    (double)g_entryWeekId);

   //--- the entry half of the cycle record has to survive a terminal restart,
   //--- otherwise a weekend reboot loses the journal row
   GlobalVariableSet(g_gvPrefix + "cWeekId",   (double)g_cycle.weekId);
   GlobalVariableSet(g_gvPrefix + "cEntryT",   (double)g_cycle.entryTime);
   GlobalVariableSet(g_gvPrefix + "cBid",      g_cycle.entryBid);
   GlobalVariableSet(g_gvPrefix + "cAsk",      g_cycle.entryAsk);
   GlobalVariableSet(g_gvPrefix + "cVol",      g_cycle.volume);
   GlobalVariableSet(g_gvPrefix + "cStopPts",  g_cycle.stopPoints);
   GlobalVariableSet(g_gvPrefix + "cRisk",     g_cycle.riskMoney);
   GlobalVariableSet(g_gvPrefix + "cLongId",   (double)g_cycle.longPosId);
   GlobalVariableSet(g_gvPrefix + "cShortId",  (double)g_cycle.shortPosId);
   GlobalVariableSet(g_gvPrefix + "cLongOpen", g_cycle.longOpen  ? 1.0 : 0.0);
   GlobalVariableSet(g_gvPrefix + "cShortOpen",g_cycle.shortOpen ? 1.0 : 0.0);
  }

double GvGet(const string key, const double fallback = 0.0)
  {
   if(GlobalVariableCheck(g_gvPrefix + key))
      return GlobalVariableGet(g_gvPrefix + key);
   return fallback;
  }

void StateLoad()
  {
   g_lastTickTime = (datetime)GvGet("lastTick");
   g_weekOpenTime = (datetime)GvGet("weekOpen");
   g_riskPoints   = GvGet("riskPts");
   g_entryWeekId  = (int)GvGet("weekId");

   g_cycle.weekId     = (int)GvGet("cWeekId");
   g_cycle.entryTime  = (datetime)GvGet("cEntryT");
   g_cycle.entryBid   = GvGet("cBid");
   g_cycle.entryAsk   = GvGet("cAsk");
   g_cycle.volume     = GvGet("cVol");
   g_cycle.stopPoints = GvGet("cStopPts");
   g_cycle.riskMoney  = GvGet("cRisk");
   g_cycle.longPosId  = (ulong)GvGet("cLongId");
   g_cycle.shortPosId = (ulong)GvGet("cShortId");
   g_cycle.longOpen   = GvGet("cLongOpen")  > 0.5;
   g_cycle.shortOpen  = GvGet("cShortOpen") > 0.5;
  }

//+------------------------------------------------------------------+
//| Calendar                                                         |
//+------------------------------------------------------------------+
int WeekId(const datetime t)
  {
   MqlDateTime s;
   TimeToStruct(t, s);
   return s.year * 100 + (s.day_of_year / 7);
  }

int SecondsIntoDay(const datetime t)
  {
   MqlDateTime s;
   TimeToStruct(t, s);
   return s.hour * 3600 + s.min * 60 + s.sec;
  }

//--- latest session close for a weekday, as seconds from midnight.
//--- false when the symbol does not trade that day at all.
bool LastSessionEnd(const ENUM_DAY_OF_WEEK day, int &endSeconds)
  {
   datetime from, to;
   bool found = false;
   endSeconds = 0;

   for(uint i = 0; i < 8; i++)
     {
      if(!SymbolInfoSessionTrade(_Symbol, day, i, from, to))
         break;
      found = true;
      int t = (int)to;
      //--- a session closing at midnight comes back as 0
      if(t == 0)
         t = 86400;
      if(t > endSeconds)
         endSeconds = t;
     }
   return found;
  }

//--- true when `day` trades but the next day does not, i.e. the week ends here
bool IsLastTradingDayOfWeek(const ENUM_DAY_OF_WEEK day, int &endSeconds)
  {
   if(!LastSessionEnd(day, endSeconds))
      return false;

   ENUM_DAY_OF_WEEK next = (ENUM_DAY_OF_WEEK)((day + 1) % 7);
   int ignored;
   return !LastSessionEnd(next, ignored);
  }

bool InEntryWindow(const datetime now)
  {
   MqlDateTime s;
   TimeToStruct(now, s);

   int endSeconds;
   if(!IsLastTradingDayOfWeek((ENUM_DAY_OF_WEEK)s.day_of_week, endSeconds))
      return false;

   int nowSeconds = SecondsIntoDay(now);
   return (nowSeconds >= endSeconds - InpEntrySeconds &&
           nowSeconds <  endSeconds - InpEntryDeadline);
  }

//+------------------------------------------------------------------+
//| Symbol maths                                                     |
//+------------------------------------------------------------------+
double VolumeStep() { return SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP); }
double VolumeMin()  { return SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);  }

double VolumeMax()
  {
   double brokerMax = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   if(InpLotCap > 0.0)
      return MathMin(brokerMax, InpLotCap);
   return brokerMax;
  }

//--- clamp into [min,max] and snap down onto whole volume steps
double NormalizeVolume(double volume)
  {
   double step = VolumeStep();
   double mn   = VolumeMin();
   double mx   = VolumeMax();

   if(step <= 0.0)
      step = 0.01;

   //--- "several counts of the minimum": whole steps only, always rounding down
   volume = MathFloor(volume / step + 1e-8) * step;

   if(volume > mx)
      volume = MathFloor(mx / step + 1e-8) * step;
   if(volume < mn)
      return 0.0;   // below the broker minimum there is no tradeable size

   int digits = (int)MathMax(0, MathRound(-MathLog10(step)));
   return NormalizeDouble(volume, digits);
  }

double StopDistancePoints()
  {
   if(!InpUseStopLoss && InpSizingMode == SIZING_MAX_MARGIN)
     {
      //--- no stop, but we still need a risk reference for the R-multiple
      //--- target and the capped-loss counterfactual: use the ATR estimate
     }

   if(InpStopLossPoints > 0.0)
      return InpStopLossPoints;

   if(g_atrHandle == INVALID_HANDLE)
      return 0.0;

   double buf[];
   if(CopyBuffer(g_atrHandle, 0, 0, 1, buf) != 1)
      return 0.0;

   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   if(point <= 0.0)
      return 0.0;

   return (buf[0] * InpAtrMultiplier) / point;
  }

//+------------------------------------------------------------------+
//| Position sizing                                                  |
//+------------------------------------------------------------------+
//--- share of free margin this account may commit.
//--- one leg per account gets the lot; a two-legged hedge splits it.
double MarginAllocation()
  {
   double free  = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   double share = (InpLegMode == LEG_BOTH) ? 0.5 : 1.0;
   return free * share * InpMarginBufferPct / 100.0;
  }

//--- biggest volume whose margin requirement still fits inside `allowance`
double MaxVolumeForMargin(const ENUM_ORDER_TYPE type, const double allowance)
  {
   if(allowance <= 0.0)
      return 0.0;

   double price = (type == ORDER_TYPE_BUY)
                  ? SymbolInfoDouble(_Symbol, SYMBOL_ASK)
                  : SymbolInfoDouble(_Symbol, SYMBOL_BID);

   double marginPerLot = 0.0;
   if(!OrderCalcMargin(type, _Symbol, 1.0, price, marginPerLot) || marginPerLot <= 0.0)
     {
      Log("OrderCalcMargin failed - cannot size position");
      return 0.0;
     }

   double volume = NormalizeVolume(allowance / marginPerLot);

   //--- the linear estimate ignores tiered margin, so verify and walk down
   double step = VolumeStep();
   for(int guard = 0; guard < 1000 && volume > 0.0; guard++)
     {
      double required = 0.0;
      if(OrderCalcMargin(type, _Symbol, volume, price, required) && required <= allowance)
         return volume;
      volume = NormalizeVolume(volume - step);
     }
   return 0.0;
  }

//--- volume whose stop-out costs exactly InpRiskPercent of equity
double VolumeForFixedRisk(const double stopPoints)
  {
   if(stopPoints <= 0.0)
      return 0.0;

   double tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   double point     = SymbolInfoDouble(_Symbol, SYMBOL_POINT);

   if(tickValue <= 0.0 || tickSize <= 0.0 || point <= 0.0)
      return 0.0;

   double lossPerLot = stopPoints * point / tickSize * tickValue;
   if(lossPerLot <= 0.0)
      return 0.0;

   double budget = AccountInfoDouble(ACCOUNT_EQUITY) * InpRiskPercent / 100.0;
   return NormalizeVolume(budget / lossPerLot);
  }

double LegVolume(const ENUM_ORDER_TYPE type, const double stopPoints)
  {
   double byMargin = MaxVolumeForMargin(type, MarginAllocation());

   if(InpSizingMode == SIZING_FIXED_RISK)
      return MathMin(VolumeForFixedRisk(stopPoints), byMargin);

   return byMargin;
  }

//--- account-currency loss if `volume` travels `stopPoints` against us
double RiskMoney(const ENUM_ORDER_TYPE type, const double volume, const double stopPoints)
  {
   if(volume <= 0.0 || stopPoints <= 0.0)
      return 0.0;

   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   double open  = (type == ORDER_TYPE_BUY)
                  ? SymbolInfoDouble(_Symbol, SYMBOL_ASK)
                  : SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double close = (type == ORDER_TYPE_BUY)
                  ? open - stopPoints * point
                  : open + stopPoints * point;

   double profit = 0.0;
   if(!OrderCalcProfit(type, _Symbol, volume, open, close, profit))
      return 0.0;

   return MathAbs(profit);
  }

//+------------------------------------------------------------------+
//| Position bookkeeping                                             |
//+------------------------------------------------------------------+
bool SelectOurPosition(const int index, ulong &ticket)
  {
   ticket = PositionGetTicket(index);
   if(ticket == 0)
      return false;
   if(PositionGetString(POSITION_SYMBOL) != _Symbol)
      return false;
   if(PositionGetInteger(POSITION_MAGIC) != (long)InpMagic)
      return false;
   return true;
  }

int CountOurPositions()
  {
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket;
      if(SelectOurPosition(i, ticket))
         count++;
     }
   return count;
  }

//+------------------------------------------------------------------+
//| Entry                                                            |
//+------------------------------------------------------------------+
void OpenCycle()
  {
   double point  = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   int    digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double stopPoints = StopDistancePoints();

   //--- LOSER_STOP_LOSS and the R-multiple target both need a risk reference
   bool needsStop = (InpUseStopLoss || InpExitMode != EXIT_TIMED);
   if(needsStop && stopPoints <= 0.0)
     {
      Log("No usable stop distance - skipping entry");
      return;
     }

   long stopLevel = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   if(stopPoints > 0.0 && stopPoints < (double)stopLevel)
     {
      stopPoints = (double)stopLevel;
      Log(StringFormat("Stop widened to the broker minimum of %d points", (int)stopLevel));
     }

   if(InpMaxSpreadPoints > 0)
     {
      long spread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
      if(spread > InpMaxSpreadPoints)
        {
         Log(StringFormat("Spread %d over limit %d - skipping entry", (int)spread, InpMaxSpreadPoints));
         return;
        }
     }

   bool wantLong  = (InpLegMode == LEG_BOTH || InpLegMode == LEG_LONG);
   bool wantShort = (InpLegMode == LEG_BOTH || InpLegMode == LEG_SHORT);

   double volume = 0.0;
   if(InpLegMode == LEG_BOTH)
     {
      //--- keep the hedge symmetric; an oversized leg is a directional bet
      volume = MathMin(LegVolume(ORDER_TYPE_BUY,  stopPoints),
                       LegVolume(ORDER_TYPE_SELL, stopPoints));
     }
   else
     {
      volume = LegVolume(wantLong ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, stopPoints);
     }

   if(volume <= 0.0)
     {
      Log("Sized to zero lots - insufficient free margin");
      return;
     }

   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   double buySl = 0.0, sellSl = 0.0, buyTp = 0.0, sellTp = 0.0;
   if(InpUseStopLoss && InpLoserMode != LOSER_HOLD_BOTH)
     {
      buySl  = NormalizeDouble(ask - stopPoints * point, digits);
      sellSl = NormalizeDouble(bid + stopPoints * point, digits);
     }
   if(InpExitMode == EXIT_R_MULTIPLE || InpExitMode == EXIT_HYBRID)
     {
      double tpPoints = stopPoints * InpTakeProfitR;
      buyTp  = NormalizeDouble(ask + tpPoints * point, digits);
      sellTp = NormalizeDouble(bid - tpPoints * point, digits);
     }

   g_trade.SetDeviationInPoints(InpSlippagePoints);

   bool okLong = true, okShort = true;
   if(wantLong)
     {
      okLong = g_trade.Buy(volume, _Symbol, 0.0, buySl, buyTp, "gap-long");
      if(!okLong)
         Log(StringFormat("Long leg rejected: %d %s",
                          g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription()));
     }
   if(wantShort)
     {
      okShort = g_trade.Sell(volume, _Symbol, 0.0, sellSl, sellTp, "gap-short");
      if(!okShort)
         Log(StringFormat("Short leg rejected: %d %s",
                          g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription()));
     }

   //--- on a two-legged account a single fill is a naked position, not a hedge
   if(InpLegMode == LEG_BOTH && okLong != okShort)
     {
      Log("Only one leg filled - unwinding to avoid a naked position");
      CloseAll("unpaired leg");
      return;
     }
   if(!okLong || !okShort)
      return;

   //--- open the cycle record
   ResetCycle();
   g_cycle.weekId     = WeekId(TimeCurrent());
   g_cycle.entryTime  = TimeCurrent();
   g_cycle.entryBid   = bid;
   g_cycle.entryAsk   = ask;
   g_cycle.volume     = volume;
   g_cycle.stopPoints = stopPoints;
   g_cycle.riskMoney  = RiskMoney(ORDER_TYPE_BUY, volume, stopPoints);
   g_cycle.longOpen   = wantLong;
   g_cycle.shortOpen  = wantShort;

   //--- remember which positions belong to this cycle
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket;
      if(!SelectOurPosition(i, ticket))
         continue;
      if(PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY)
         g_cycle.longPosId  = PositionGetInteger(POSITION_IDENTIFIER);
      else
         g_cycle.shortPosId = PositionGetInteger(POSITION_IDENTIFIER);
     }

   g_riskPoints   = stopPoints;
   g_weekOpenTime = 0;
   g_entryWeekId  = g_cycle.weekId;
   StateSave();

   Log(StringFormat("Opened %s: %.2f lots, stop %.1f pts, risk %.2f per leg, equity %.2f",
                    EnumToString(InpLegMode), volume, stopPoints,
                    g_cycle.riskMoney, AccountInfoDouble(ACCOUNT_EQUITY)));
  }

//+------------------------------------------------------------------+
//| Exit                                                             |
//+------------------------------------------------------------------+
void CloseAll(const string reason)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket;
      if(!SelectOurPosition(i, ticket))
         continue;
      if(!g_trade.PositionClose(ticket, InpSlippagePoints))
         Log(StringFormat("Close of #%I64u failed: %d %s", ticket,
                          g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription()));
     }
   Log("Flattened: " + reason);
  }

void CloseLosingLeg()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket;
      if(!SelectOurPosition(i, ticket))
         continue;

      if(PositionGetDouble(POSITION_PROFIT) < 0.0)
        {
         if(g_trade.PositionClose(ticket, InpSlippagePoints))
            Log(StringFormat("Losing leg #%I64u closed at the open", ticket));
         else
            Log(StringFormat("Could not close losing leg #%I64u: %d", ticket, g_trade.ResultRetcode()));
        }
     }
  }

void ApplyBreakEven()
  {
   if(InpBreakEvenR <= 0.0 || g_riskPoints <= 0.0)
      return;

   double point   = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   int    digits  = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double trigger = g_riskPoints * InpBreakEvenR * point;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket;
      if(!SelectOurPosition(i, ticket))
         continue;

      double open = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl   = PositionGetDouble(POSITION_SL);
      double tp   = PositionGetDouble(POSITION_TP);
      long   type = PositionGetInteger(POSITION_TYPE);

      if(type == POSITION_TYPE_BUY)
        {
         if(SymbolInfoDouble(_Symbol, SYMBOL_BID) - open >= trigger && sl < open)
            g_trade.PositionModify(ticket, NormalizeDouble(open, digits), tp);
        }
      else
        {
         if(open - SymbolInfoDouble(_Symbol, SYMBOL_ASK) >= trigger && (sl > open || sl == 0.0))
            g_trade.PositionModify(ticket, NormalizeDouble(open, digits), tp);
        }
     }
  }

//+------------------------------------------------------------------+
//| Weekend gap detection                                            |
//+------------------------------------------------------------------+
void DetectWeekOpen(const datetime now)
  {
   if(g_lastTickTime == 0 || g_weekOpenTime != 0)
      return;
   if(CountOurPositions() == 0)
      return;

   if(now - g_lastTickTime >= WEEKEND_GAP_SECONDS)
     {
      g_weekOpenTime      = now;
      g_cycle.reopenTime  = now;
      g_cycle.reopenBid   = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      g_cycle.reopenAsk   = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      StateSave();

      double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
      double gap = 0.0;
      if(point > 0.0 && g_cycle.entryBid > 0.0)
         gap = ((g_cycle.reopenBid + g_cycle.reopenAsk) / 2.0
                - (g_cycle.entryBid + g_cycle.entryAsk) / 2.0) / point;

      Log(StringFormat("Market reopened after %d minutes, gap %.1f points",
                       (int)((now - g_lastTickTime) / 60), gap));
     }
  }

//+------------------------------------------------------------------+
//| Open position management                                         |
//+------------------------------------------------------------------+
void ManagePositions(const datetime now)
  {
   if(g_weekOpenTime == 0)
      return;

   if(InpLoserMode == LOSER_CLOSE_OPEN && InpLegMode == LEG_BOTH)
      CloseLosingLeg();

   if(InpExitMode == EXIT_HYBRID)
      ApplyBreakEven();

   //--- the timed exit; the R-multiple target rides on the order's TP
   if(InpExitMode == EXIT_TIMED || InpExitMode == EXIT_HYBRID)
     {
      if(now - g_weekOpenTime >= (datetime)(InpTimedExitMinutes * 60))
         CloseAll(StringFormat("timed exit, %d minutes after reopen", InpTimedExitMinutes));
     }
  }

//+------------------------------------------------------------------+
//| Trade transaction journal                                        |
//|                                                                  |
//| Every deal is recorded, and closing deals fill in the cycle       |
//| record. DEAL_REASON is what tells you whether the exit was a      |
//| stop loss, a take profit, a stop-out or the EA itself.            |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest     &request,
                        const MqlTradeResult      &result)
  {
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD)
      return;
   if(!HistoryDealSelect(trans.deal))
      return;
   if(HistoryDealGetString(trans.deal, DEAL_SYMBOL) != _Symbol)
      return;
   if(HistoryDealGetInteger(trans.deal, DEAL_MAGIC) != (long)InpMagic)
      return;

   long   entry      = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
   long   dealType   = HistoryDealGetInteger(trans.deal, DEAL_TYPE);
   ulong  positionId = (ulong)HistoryDealGetInteger(trans.deal, DEAL_POSITION_ID);
   double price      = HistoryDealGetDouble(trans.deal, DEAL_PRICE);
   double profit     = HistoryDealGetDouble(trans.deal, DEAL_PROFIT);
   double commission = HistoryDealGetDouble(trans.deal, DEAL_COMMISSION);
   double swap       = HistoryDealGetDouble(trans.deal, DEAL_SWAP);
   string reason     = EnumToString((ENUM_DEAL_REASON)HistoryDealGetInteger(trans.deal, DEAL_REASON));

   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   CsvAppend(g_dealFile, DealHeader(), StringFormat(
      "%s,%s,%I64u,%I64u,%s,%s,%.2f,%.*f,%.*f,%.*f,%.2f,%.2f,%.2f,%s,%s",
      TimeToString((datetime)HistoryDealGetInteger(trans.deal, DEAL_TIME), TIME_DATE | TIME_SECONDS),
      _Symbol, trans.deal, positionId,
      EnumToString((ENUM_DEAL_TYPE)dealType),
      EnumToString((ENUM_DEAL_ENTRY)entry),
      HistoryDealGetDouble(trans.deal, DEAL_VOLUME),
      digits, price,
      digits, HistoryDealGetDouble(trans.deal, DEAL_SL),
      digits, HistoryDealGetDouble(trans.deal, DEAL_TP),
      profit, commission, swap, reason,
      HistoryDealGetString(trans.deal, DEAL_COMMENT)));

   if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY)
      return;

   //--- a closing deal: fold the result into the cycle record.
   //--- the deal that closes a long is a sell, hence the inverted test.
   double net = profit + commission + swap;
   if(dealType == DEAL_TYPE_SELL)
     {
      g_cycle.longExitPrice = price;
      g_cycle.longProfit   += net;
      g_cycle.longReason    = reason;
      g_cycle.longOpen      = false;
     }
   else
     {
      g_cycle.shortExitPrice = price;
      g_cycle.shortProfit   += net;
      g_cycle.shortReason    = reason;
      g_cycle.shortOpen      = false;
     }

   if(!g_cycle.longOpen && !g_cycle.shortOpen && g_cycle.entryTime > 0)
     {
      WriteCycle();
      ResetCycle();
      StateSave();
     }
  }

//+------------------------------------------------------------------+
//| Lifecycle                                                        |
//+------------------------------------------------------------------+
int OnInit()
  {
   g_gvPrefix   = StringFormat("WGH_%s_%I64u_", _Symbol, InpMagic);
   g_cycleFile  = StringFormat("WGH_cycles_%s_%I64u.csv", _Symbol, InpMagic);
   g_dealFile   = StringFormat("WGH_deals_%s_%I64u.csv",  _Symbol, InpMagic);

   //--- a netting account nets the two legs into a single position, so the
   //--- hedge silently becomes nothing at all. Refuse rather than mislead.
   if(InpLegMode == LEG_BOTH)
     {
      ENUM_ACCOUNT_MARGIN_MODE mode =
         (ENUM_ACCOUNT_MARGIN_MODE)AccountInfoInteger(ACCOUNT_MARGIN_MODE);
      if(mode != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
        {
         Print("FATAL: LEG_BOTH needs a hedging account. On this netting account a "
               "buy and a sell on the same symbol would collapse into one position. "
               "Use LEG_LONG or LEG_SHORT with a second account instead.");
         return INIT_FAILED;
        }
     }

   if(!SymbolInfoInteger(_Symbol, SYMBOL_SELECT))
     {
      Print("FATAL: symbol not selected in Market Watch");
      return INIT_FAILED;
     }

   if((ENUM_SYMBOL_TRADE_MODE)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_MODE) != SYMBOL_TRADE_MODE_FULL)
     {
      Print("FATAL: symbol does not allow full trading");
      return INIT_FAILED;
     }

   if(InpStopLossPoints <= 0.0)
     {
      g_atrHandle = iATR(_Symbol, PERIOD_H1, InpAtrPeriod);
      if(g_atrHandle == INVALID_HANDLE)
        {
         Print("FATAL: could not create the ATR handle for the automatic stop");
         return INIT_FAILED;
        }
     }

   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(InpSlippagePoints);
   g_trade.SetTypeFillingBySymbol(_Symbol);

   ResetCycle();
   StateLoad();

   Log(StringFormat("Initialised. Leg=%s Sizing=%s Loser=%s Exit=%s Journal=%s",
                    EnumToString(InpLegMode), EnumToString(InpSizingMode),
                    EnumToString(InpLoserMode), EnumToString(InpExitMode), g_cycleFile));
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   if(g_atrHandle != INVALID_HANDLE)
      IndicatorRelease(g_atrHandle);
   StateSave();
  }

void OnTick()
  {
   datetime now = TimeCurrent();

   DetectWeekOpen(now);

   if(CountOurPositions() > 0)
     {
      ManagePositions(now);
     }
   else
     {
      //--- flat again: the cycle is complete, arm for next week
      if(g_weekOpenTime != 0)
        {
         g_weekOpenTime = 0;
         g_riskPoints   = 0;
         StateSave();
        }

      bool weekAlreadyTraded = (WeekId(now) == g_entryWeekId);
      bool allowedToTrade    = InpAllowMultiWeek || g_entryWeekId == 0;

      if(!weekAlreadyTraded && allowedToTrade && InEntryWindow(now))
         OpenCycle();
     }

   g_lastTickTime = now;
   StateSave();
  }
//+------------------------------------------------------------------+
