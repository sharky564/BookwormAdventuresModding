CreatureBaseClass = {}
function CreatureBaseClass:ApplyResistance(amt)
  if gBattleEngine == nil then
    return amt
  end
  if self ~= gBattleEngine.mEnemyPtr then
    return amt
  end
  if gBookManager == nil or gBookManager.mSelectedBook == nil then
    return amt
  end
  local lBook = gBookManager.mSelectedBook.mBookNum
  if lBook < 2 then
    return amt
  end
  local lChapObj = gBookManager.mSelectedBook.mSelectedChapter
  local lChap = -1
  if lChapObj ~= nil and lChapObj.mChapterNumber ~= nil then
    lChap = lChapObj.mChapterNumber
  end
  local lStage = 1
  if gBattleEngine.mStageNum ~= nil then
    lStage = gBattleEngine.mStageNum
  end
  local lOffset
  if lBook == 2 then
    lOffset = {0, 6, 12, 17, 18, 23, 28, 33, 38, 43}
  else
    lOffset = {48, 53, 58, 63, 68, 73, 78, 82, 87, 92}
  end
  local lIdx = lChap - 1
  local lBase = lOffset[lIdx]
  if lBase == nil then
    lBase = lOffset[9]
  end
  local lNum = lBase + lStage - 1
  if lNum < 0 then
    lNum = 0
  end
  local lResist = 1 + 0.04 * lNum
  if lResist > 1 then
    amt = common.DecimalFloor(amt / lResist, 0.25)
    if amt < 0.25 then
      amt = 0.25
    end
  end
  return amt
end
