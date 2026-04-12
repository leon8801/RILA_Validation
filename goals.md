1. Transaction types to handle
    ✅Initial Premium
    ✅Issue
    ✅Feature Change
    ✅Processing Date 
    ✅Index Valuation
    ✅Withdrawal(s)
    ✅Annual Processing
    ✅Reallocation Transfer
    ✅Freelook
    ✅ROP DB fee
    ✅Rate Renewal
    ✅Performance Captures/Locks
    ✅Death Benefit
    ✅Account Value Adjustement   
    ✅Autolock
    ✅Auto Performance Capture

2. Additional features to add
    ✅Standalone data input excel UI to get fund/strategy mapping info
    ✅Straighten data channel to SQL query outputs to Python program
    ✅OUV & VIX data channel
    ✅Interim value calc (AV/MWV/StratBase) based on OUV & VIX
    ✅Checking split in txn amounts on withdrawals and fees
    ✅Tracking, validating surr charge and splitting the charge to fund level 
    ✅Auto performance capture (OUV history max)
    ✅Auto lock (Equity index history max)
    Strategy Return Calculation types:
        ✅Buffer
        ✅Cap
        ✅Participation
        ✅Fixed account crediting
        ✅Dual Direction
        ✅Enhanced Par & Cap
        ✅Lock
        ✅Trigger
        🔜Cap Secure (can wait since no policy has reached term end yet)
        
    🔜Statement compare program 
    ✅statement cycle day run mode (single day)
    ✅Edit query to get single column index rates
    ✅Edit query to get distinct field headers                            
    ✅Add data pull piece from Mahendra's                         

3. QoL improvement items


Questions for Mahendra:


sort order improved in post process


AS compare items:
1. Contract Summary (pol level)
    ✅Contract Value as of previous anniversary
    ✅Contract Value as of current anniversary
    ✅Premium since issue (cumulative)
    ✅Premium This Statement Period
    ✅Withdrawals/Charges	Since Issue	
    ✅Withdrawals/Charges	This Statement Period	
    ✅Interest Credited Since Issue	
    ✅Interest Credited This Statement Period	
    🔜Change in Market Value Since Issue	
    🔜Change in Market Value This Statement Period	
2. Allocation Details (fund level)
    For each fund
        ✅Fund Name
        ✅Term Start Date
        ✅Term End Date
        ✅Contract value at current anniversary
        ✅Alloc %
        ✅Buffer
        ✅Upside Param type(cap/trig/lock)
        ✅Upside Param rate
3. Contract Value - Locked Funds (fund level)
    For each lock fund
        Fund name
        AV locked
        Lock Fixed Rate
        Int credited (on fixed rate)
        AV locked + Int credited
4. ContractValue-MidTermFunds (fund level)
    For each non terminated multi year funds
        Fund name
        Strat Base
        Interim (account) value
5. ContractValue-EndofTermfunds (fund level)
    For each fund that terminates on this anniversary
        Fund Name
        Strat Base
        Index Credited %
        Interest credited
        Ending value
        <!-- Strat Base * Index Credit % = Interest Credited  -->
        <!-- Strat Base + Interest Credited = Ending Value -->
6. PerformanceSummary (fund level)
    For each fund
        ✅Fund Name
        ✅Term Start Date
        ✅Term End Date
        ✅Beginning equity index value
        ✅Ending equity index value
        ✅Raw index change %
        ✅Upside Param rate (cap/trig/lock)
        ✅Downside param rate (buffer)
        ✅Index Credited %
7. Transaction Summary (Txn - Fund Level) Txn code in (11,18,44,90,170,71,91,92,93)
    For each txn
        For each fund
            Txn Date
            Txn Type Desc
            Txn Amt
            Fund Name
            Term Start Date
            Term End Date
            Amt Locked/Captured
8. ImpInfo-ContractDetails (pol level)
    ✅AV
    ✅MWV
    ✅Cash Value
    ✅Death Ben
9. PerfAutoCapture Program details
10. PerfCapture & Lock Details
    
    