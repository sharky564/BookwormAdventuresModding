TileEngine = {}
function TileEngine:NextNeeded(t, c)
  local lForce = false
  if gBookManager ~= nil and gBookManager.mSelectedBook ~= nil then
    if gBookManager.mSelectedBook.mSelectedChapter ~= nil then
      lForce = true
    end
  end
  if lForce then
    local cM,cI,cS,cU,cN,cD,cE,cR,cT,cA,cG = 0,0,0,0,0,0,0,0,0,0,0
    for k, tt in pairs(gTileTable) do
      if k ~= "n" and k ~= t then
        local L = tt.mLetter
        if L == "M" then cM = cM + 1
        elseif L == "I" then cI = cI + 1
        elseif L == "S" then cS = cS + 1
        elseif L == "U" then cU = cU + 1
        elseif L == "N" then cN = cN + 1
        elseif L == "D" then cD = cD + 1
        elseif L == "E" then cE = cE + 1
        elseif L == "R" then cR = cR + 1
        elseif L == "T" then cT = cT + 1
        elseif L == "A" then cA = cA + 1
        elseif L == "G" then cG = cG + 1
        end
      end
    end
    if cM < 1 then return "M"
    elseif cI < 2 then return "I"
    elseif cS < 2 then return "S"
    elseif cU < 1 then return "U"
    elseif cN < 3 then return "N"
    elseif cD < 2 then return "D"
    elseif cE < 1 then return "E"
    elseif cR < 1 then return "R"
    elseif cT < 1 then return "T"
    elseif cA < 1 then return "A"
    elseif cG < 1 then return "G"
    else return "N"
    end
  end
  return c
end
