//+------------------------------------------------------------------+
//|  CalendarExport.mq5                                              |
//|  fxmacro — Forex Macro Event Impact Explorer                     |
//|                                                                  |
//|  Exports the terminal's built-in economic calendar to CSV for    |
//|  the Python side to read.  See planning.md §4.2 Route A.         |
//|                                                                  |
//|  Why this script exists at all: the Python MetaTrader5 package   |
//|  exposes prices, symbols, orders, positions and history, and no  |
//|  calendar function whatsoever.  An MQL5 script writing CSV into  |
//|  MQL5\Files is the standard bridge.                              |
//|                                                                  |
//|  Deliberate choices:                                             |
//|                                                                  |
//|  * Values are written RAW — still multiplied by 1,000,000, with  |
//|    LONG_MIN left in place for unset fields.  The Python          |
//|    normaliser undoes both, because that is where unit tests can  |
//|    reach the arithmetic.  Converting here would hide the trap.   |
//|                                                                  |
//|  * Output is UTF-8 written through FILE_BIN.  The terminal       |
//|    localises event names, so an ANSI write mangles every         |
//|    non-English install, and FILE_UNICODE would emit UTF-16 that  |
//|    Python's csv module reads as NULs.                            |
//|                                                                  |
//|  * The server's GMT offset at export time is written on every    |
//|    row.  It is NOT applied here.  Server DST rules have changed  |
//|    over the years, so applying today's offset to a 2009 event is |
//|    an assumption; recording it lets the Python side say so.      |
//|                                                                  |
//|  Usage: drop in MQL5\Scripts, compile (F7), run on any chart.    |
//|  Tools → Options → Server → "Enable news" must be ticked, or the |
//|  calendar is empty.                                              |
//+------------------------------------------------------------------+
#property script_show_inputs
#property strict

input datetime InpFrom       = D'2007.01.01 00:00';  // export events from
input datetime InpTo         = 0;                    // 0 = up to now
input string   InpCurrencies = "USD,EUR,GBP,JPY,CHF,AUD,CAD,NZD";  // blank = all
input string   InpFileName   = "fxmacro_calendar.csv";

//--- CSV is semicolon-delimited: event names contain commas.
const string SEP = ";";

//+------------------------------------------------------------------+
string CsvEscape(const string value)
  {
   string out = value;
   StringReplace(out, "\r", " ");
   StringReplace(out, "\n", " ");
   StringReplace(out, SEP, ",");
   return out;
  }

//+------------------------------------------------------------------+
string ImportanceName(const ENUM_CALENDAR_EVENT_IMPORTANCE importance)
  {
   switch(importance)
     {
      case CALENDAR_IMPORTANCE_LOW:      return "LOW";
      case CALENDAR_IMPORTANCE_MODERATE: return "MODERATE";
      case CALENDAR_IMPORTANCE_HIGH:     return "HIGH";
      default:                           return "NONE";
     }
  }

//+------------------------------------------------------------------+
//| Raw long, or empty when the terminal says "unset".               |
//| LONG_MIN is written through as-is so the Python side can prove   |
//| it handles it; empty means the field was absent entirely.        |
//+------------------------------------------------------------------+
string RawLong(const long value)
  {
   return IntegerToString(value);
  }

//+------------------------------------------------------------------+
bool WriteUtf8(const int handle, const string text)
  {
   uchar bytes[];
   int count = StringToCharArray(text, bytes, 0, -1, CP_UTF8);
   if(count <= 0)
      return false;
   //--- StringToCharArray appends a terminating zero; do not write it.
   return FileWriteArray(handle, bytes, 0, count - 1) == (uint)(count - 1);
  }

//+------------------------------------------------------------------+
void OnStart()
  {
   datetime from = InpFrom;
   datetime to   = (InpTo == 0) ? TimeTradeServer() + 60 * 60 * 24 * 30 : InpTo;

   //--- The offset in force right now. Recorded, never applied (see header).
   long gmtOffset = (long)(TimeTradeServer() - TimeGMT());

   string wanted = InpCurrencies;
   StringTrimLeft(wanted);
   StringTrimRight(wanted);
   string currencies[];
   int currencyCount = (StringLen(wanted) > 0)
                       ? StringSplit(wanted, ',', currencies)
                       : 0;

   int handle = FileOpen(InpFileName, FILE_WRITE | FILE_BIN);
   if(handle == INVALID_HANDLE)
     {
      PrintFormat("CalendarExport: cannot open %s (error %d)", InpFileName, GetLastError());
      return;
     }

   WriteUtf8(handle,
             "event_id" + SEP + "event_name" + SEP + "country" + SEP + "currency" + SEP +
             "importance" + SEP + "server_time" + SEP + "server_gmt_offset" + SEP +
             "period" + SEP + "revision" + SEP + "actual_value" + SEP +
             "forecast_value" + SEP + "prev_value" + SEP + "revised_prev_value" + SEP +
             "unit" + SEP + "multiplier" + SEP + "digits" + SEP + "event_code" + SEP +
             "source_url" + "\r\n");

   int written = 0;
   int skipped = 0;

   for(int c = 0; c < MathMax(currencyCount, 1); c++)
     {
      string currencyFilter = (currencyCount > 0) ? currencies[c] : NULL;
      if(currencyCount > 0)
        {
         StringTrimLeft(currencyFilter);
         StringTrimRight(currencyFilter);
        }

      MqlCalendarValue values[];
      int total = CalendarValueHistory(values, from, to, NULL, currencyFilter);
      if(total <= 0)
        {
         PrintFormat("CalendarExport: no values for %s (error %d)",
                     (currencyCount > 0 ? currencyFilter : "ALL"), GetLastError());
         continue;
        }

      for(int i = 0; i < total; i++)
        {
         MqlCalendarEvent event;
         if(!CalendarEventById(values[i].event_id, event))
           {
            skipped++;
            continue;
           }

         MqlCalendarCountry country;
         string countryName = "";
         string countryCurrency = "";
         if(CalendarCountryById(event.country_id, country))
           {
            countryName     = country.name;
            countryCurrency = country.currency;
           }

         string line =
            IntegerToString((long)values[i].event_id) + SEP +
            CsvEscape(event.name) + SEP +
            CsvEscape(countryName) + SEP +
            CsvEscape(countryCurrency) + SEP +
            ImportanceName((ENUM_CALENDAR_EVENT_IMPORTANCE)event.importance) + SEP +
            TimeToString(values[i].time, TIME_DATE | TIME_SECONDS) + SEP +
            IntegerToString(gmtOffset) + SEP +
            TimeToString(values[i].period, TIME_DATE | TIME_SECONDS) + SEP +
            IntegerToString(values[i].revision) + SEP +
            RawLong(values[i].actual_value) + SEP +
            RawLong(values[i].forecast_value) + SEP +
            RawLong(values[i].prev_value) + SEP +
            RawLong(values[i].revised_prev_value) + SEP +
            CsvEscape(event.unit) + SEP +
            IntegerToString(event.multiplier) + SEP +
            IntegerToString(event.digits) + SEP +
            CsvEscape(event.event_code) + SEP +
            CsvEscape(event.source_url) + "\r\n";

         if(WriteUtf8(handle, line))
            written++;
         else
            skipped++;
        }
     }

   FileClose(handle);

   PrintFormat("CalendarExport: wrote %d rows to MQL5\\Files\\%s (%d skipped). "
               "Server GMT offset recorded as %d seconds.",
               written, InpFileName, skipped, (int)gmtOffset);
   PrintFormat("CalendarExport: import it from the fxmacro console — "
               "Sources → MetaTrader 5 calendar → Upload, or point the source's "
               "export_dir at this folder.");
  }
//+------------------------------------------------------------------+
