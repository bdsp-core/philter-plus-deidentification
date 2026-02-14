# Distributed Processing for 500M Records

## Time Estimates

### Single Machine (Current Setup)
- **Hardware:** 192GB RAM, 32 cores/64 threads
- **Configuration:** 60 workers, batch size 150
- **Expected Speed:** 80-120 records/sec
- **Time for 500M:** **48-72 days**

### Multi-Machine Setup (Recommended)
- **3 identical machines** processing in parallel
- **Combined Speed:** 240-360 records/sec
- **Time for 500M:** **16-24 days** ✓

### 5-Machine Setup (If available)
- **Combined Speed:** 400-600 records/sec
- **Time for 500M:** **10-14 days** ✓✓

## Distributed Processing Strategy

### Option 1: Partition by NoteCSNID Range (Recommended)

Split the work by ID ranges across machines:

**Machine 1:**
```python
# Process NoteCSNID: 0 to 100,000,000
WHERE NoteCSNID >= 0 AND NoteCSNID < 100000000
```

**Machine 2:**
```python
# Process NoteCSNID: 100,000,000 to 200,000,000
WHERE NoteCSNID >= 100000000 AND NoteCSNID < 200000000
```

**Machine 3:**
```python
# Process NoteCSNID: 200,000,000 to 300,000,000
WHERE NoteCSNID >= 200000000 AND NoteCSNID < 300000000
```

And so on...

#### Implementation

Edit `db_deidentify_massive_scale.py` and add these parameters:

```python
# At the top of main()
# DISTRIBUTED PROCESSING CONFIG
MACHINE_ID = 1  # Change to 1, 2, 3, etc. for each machine
TOTAL_MACHINES = 3  # Total number of machines
ID_RANGE_SIZE = 200000000  # Adjust based on your max NoteCSNID

# Calculate this machine's range
id_start = (MACHINE_ID - 1) * ID_RANGE_SIZE
id_end = MACHINE_ID * ID_RANGE_SIZE
```

Then modify the fetch query:

```python
query = f"""
    SELECT TOP {fetch_batch_size}
        [{note_id_column}],
        [{deid_name_column}],
        [{text_column}],
        [{shifted_year_column}]
    FROM {source_table}
    WHERE [{note_id_column}] >= {id_start}
      AND [{note_id_column}] < {id_end}
      AND [{note_id_column}] > ?
      AND [{text_column}] IS NOT NULL
      AND DATALENGTH([{text_column}]) > 0
    ORDER BY [{note_id_column}]
"""
```

### Option 2: Modulo Partitioning

Distribute records using modulo operation:

**Machine 1:** Process records where `NoteCSNID % 3 = 0`
**Machine 2:** Process records where `NoteCSNID % 3 = 1`
**Machine 3:** Process records where `NoteCSNID % 3 = 2`

```sql
WHERE NoteCSNID % 3 = 0  -- Machine 1
WHERE NoteCSNID % 3 = 1  -- Machine 2
WHERE NoteCSNID % 3 = 2  -- Machine 3
```

**Pros:** More even distribution
**Cons:** Slightly less efficient indexing

## Database Optimization for Massive Scale

### 1. Ensure Indexes Exist

```sql
-- Index on source table
CREATE NONCLUSTERED INDEX IX_NoteCSNID
ON bdsp_prod.Clinical.to_deIdentify_notes (NoteCSNID)
INCLUDE (DeIDNoteID, NoteTXT, ShiftedContactYear)

-- Index on target table for duplicate checking
CREATE NONCLUSTERED INDEX IX_Target_NoteCSNID
ON bdsp_opendata.Clinical.bdsp_notes_deid (NoteCSNID)
```

### 2. Database Server Optimization

Check with your DBA about:
- **Read uncommitted isolation level** (for faster selects, if acceptable)
- **Disable triggers** on target table during bulk load
- **Minimal logging** for bulk inserts
- **Separate disk for transaction log**
- **Increase max memory** for SQL Server

### 3. Network Optimization

- Ensure gigabit (1Gbps) or faster network
- If possible, run processing on same server as database
- Consider dedicated network VLAN for DB traffic

## Hardware Recommendations

### If You Have Access to More Machines

Ideal machine specs for additional nodes:
- **RAM:** 64GB minimum (128GB+ ideal)
- **CPU:** 16+ cores
- **Network:** 1Gbps+
- **Storage:** Not critical (data is in DB)

### Cloud Alternative

If you have cloud budget, consider:
- **AWS EC2 c6i.16xlarge** (64 vCPUs, 128GB RAM) × 3 machines
- **Cost:** ~$2.40/hour per machine = $7.20/hour for 3
- **Total cost for 20 days:** ~$3,456
- **Benefit:** Much faster completion (20 days vs 60+ days)

## Configuration for Maximum Speed

Edit `db_deidentify_massive_scale.py`:

```python
# For maximum speed on your hardware:
num_workers = 60        # Use 60 workers (pushes CPU hard)
batch_size = 200        # Increase to 200 for better efficiency
fetch_batch_size = 30000  # Larger fetches (if DB can handle)
commit_every = 2000     # Less frequent commits
checkpoint_every = 500000  # Checkpoint every 500K
report_every = 100000   # Reduce logging overhead
```

## Monitoring During Production Run

### 1. CPU Usage
```bash
# On Windows
wmic cpu get loadpercentage

# Target: 85-95% usage
# If lower: Increase num_workers
# If 100% with slowdown: Decrease slightly
```

### 2. Memory Usage
```bash
# On Windows
wmic OS get FreePhysicalMemory,TotalVisibleMemorySize

# Target: 60-80% usage
# If > 90%: Reduce num_workers or batch_size
```

### 3. Database Server
- Monitor CPU and disk I/O on SQL Server
- If SQL Server is bottleneck, consider:
  - Read replica for source table
  - Faster disks (SSD/NVMe)
  - More RAM for SQL Server

### 4. Progress Tracking

The script saves checkpoints every 100K records:
```bash
# View checkpoint
cat deidentify_checkpoint.json

# Shows:
# - Last processed NoteCSNID
# - Total processed count
# - Timestamp
```

## Estimated Timeline for 500M Records

### Conservative (80 rec/sec per machine)

| Machines | Combined Speed | Days |
|----------|---------------|------|
| 1 | 80 rec/sec | 72 days |
| 2 | 160 rec/sec | 36 days |
| 3 | 240 rec/sec | 24 days |
| 5 | 400 rec/sec | 14 days |

### Realistic (100 rec/sec per machine)

| Machines | Combined Speed | Days |
|----------|---------------|------|
| 1 | 100 rec/sec | 58 days |
| 2 | 200 rec/sec | 29 days |
| 3 | 300 rec/sec | 19 days |
| 5 | 500 rec/sec | 12 days |

### Optimistic (120 rec/sec per machine)

| Machines | Combined Speed | Days |
|----------|---------------|------|
| 1 | 120 rec/sec | 48 days |
| 2 | 240 rec/sec | 24 days |
| 3 | 360 rec/sec | 16 days |
| 5 | 600 rec/sec | 10 days |

## Production Run Checklist

- [ ] Database indexes created
- [ ] Test run completed successfully (1M records)
- [ ] Performance benchmarks measured
- [ ] Checkpoint system tested
- [ ] Backup plan for database failures
- [ ] Monitoring in place (CPU, RAM, DB)
- [ ] Alert system for errors
- [ ] Estimated completion date calculated
- [ ] Stakeholders notified of timeline
- [ ] Plan for handling errors/failures

## Troubleshooting

### "Database connection lost"
- Add connection retry logic
- Check SQL Server max connections
- Reduce num_workers if DB is overwhelmed

### "Out of memory"
- Reduce num_workers to 40-48
- Reduce batch_size to 100-120
- Check for memory leaks in long runs

### "Speed degradation over time"
- Normal as target table grows (duplicate checking slower)
- Consider partitioning target table by date ranges
- Periodically rebuild indexes

### "Inconsistent speed"
- Database maintenance windows?
- Network congestion?
- Other processes competing for resources?

## Next Steps

1. **Run test:** Process 1-10M records to benchmark actual speed
2. **Measure:** Determine if DB or CPU is bottleneck
3. **Decide:** Single machine vs distributed
4. **Configure:** Adjust parameters based on test results
5. **Execute:** Start production run with monitoring
6. **Monitor:** Check progress daily, adjust if needed
